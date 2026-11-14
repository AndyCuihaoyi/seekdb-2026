#!/usr/bin/env python3
"""Profile the approximate query with a filter, and say where the time goes.

The approximate query with a filter is several times slower than it was
in the version that passed the admission, on the same data. This
script runs the query under `perf` and reports the functions that the
time is spent in, so that the cost is attributed to a function rather
than to a guess.

Usage:
    tools/profile_ann_query.py --binary PATH [--rows N] [--text T]
                                [--dim D] [--out FILE]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


def build(binary, data_dir, rows, dim, batch, text):
    """Write the data and build the index, then leave the process alone."""
    import json
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    proc.stdout.readline()

    def ask(sql, mode="exact"):
        proc.stdin.write(
            json.dumps({"id": 1, "op": "execute", "sql": sql,
                        "search_mode": mode}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    ask("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
        "body VARCHAR(%d), v VECTOR(%d));" % (text, dim))
    body = "x" * text
    for first in range(0, rows, batch):
        values = []
        for row_id in range(first, min(first + batch, rows)):
            vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(dim))
            values.append(
                "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
            )
        ask("INSERT INTO t VALUES " + ",".join(values) + ";")
    ask("CREATE VECTOR INDEX idx_t ON t(v);")
    proc.stdin.close()
    proc.wait(timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=400000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--out", default="/tmp/perf-ann.data")
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-perf-")
    data_dir = os.path.join(work, "data")
    try:
        build(args.binary, data_dir, args.rows, args.dim, args.batch, args.text)
        probe = ",".join(str(i % 100) for i in range(args.dim))
        sql = (
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
        )
        # The command of the profile: the query is repeated, so that the
        # samples of the profile are dominated by it.
        command = [
            "perf", "record", "-q", "-g", "--call-graph=dwarf", "-F", "999",
            "-o", args.out, "--",
            args.binary, "--data-dir", data_dir,
        ]
        print("The profile is taken of a separate process; the query is run "
              "by the driver below.")
        # A driver that runs the query repeatedly, so that `perf` can
        # attach to it.
        driver = os.path.join(work, "driver.py")
        with open(driver, "w", encoding="utf-8") as handle:
            handle.write(
                "import json, sys, time\n"
                "p = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE,"
                " stdout=subprocess.PIPE, text=True, bufsize=1)\n"
                "p.stdout.readline()\n"
                "def ask(sql, mode):\n"
                "    p.stdin.write(json.dumps({'id':1,'op':'execute','sql':sql,"
                "'search_mode':mode})+'\\n'); p.stdin.flush()\n"
                "    return json.loads(p.stdout.readline())\n"
                "sql = sys.argv[-1]\n"
                "for i in range(int(sys.argv[-2])):\n"
                "    ask(sql, 'ann')\n"
                "p.stdin.close(); p.wait()\n"
            )
        # The profile of the query itself.
        prof = subprocess.Popen(
            ["perf", "record", "-q", "-g", "--call-graph=dwarf", "-F", "999",
             "-o", args.out, "--",
             args.binary, "--data-dir", data_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        prof.stdout.readline()
        for _ in range(args.repeat):
            prof.stdin.write(
                json.dumps({"id": 1, "op": "execute", "sql": sql,
                            "search_mode": "ann"}) + "\n")
            prof.stdin.flush()
            prof.stdout.readline()
        prof.stdin.close()
        prof.wait(timeout=300)

        print(subprocess.run(
            ["perf", "report", "--stdio", "-i", args.out, "--no-children",
             "--percent-limit", "1.5"],
            capture_output=True, text=True,
        ).stdout[:6000])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
