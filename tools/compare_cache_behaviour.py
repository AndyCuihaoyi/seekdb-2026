#!/usr/bin/env python3
"""Report the cache behaviour of the approximate scan, for both builds.

The script writes the same data to both builds, runs the same
approximate query, and reads the counters that the database printed:
the number of scalar blocks it decoded, the number of times it served
one from the cache, and the number of vector blocks it decoded.

The numbers say how the cache behaved, which is what tells a query that
is slow because of the *work* apart a query that is slow because of the
*cache*.

Usage:
    tools/compare_cache_behaviour.py --a PATH --b PATH [--rows N]
                                     [--text T] [--dim D]
"""

import argparse
import json
import os
import shutil
import sys
import re
import shutil
import subprocess
import tempfile
import time


STATS_RE = re.compile(
    r"ann scan cells=(\d+) rows=(\d+) max_radius=([\d.]+) "
    r"radius_sum=([\d.]+) largest_cell=(\d+)"
)
DECODE_RE = re.compile(
    r"scalar_decodes=(\d+) hits=(\d+) vector_decodes=(\d+)"
)


def run(binary, rows, dim, batch, text):
    work = tempfile.mkdtemp(prefix="mvdb-cache-")
    data_dir = os.path.join(work, "data")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    body = "x" * text
    result = {"binary": binary}
    try:
        proc = subprocess.Popen(
            [binary, "--data-dir", data_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(log_path, "wb"), text=True, bufsize=1,
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
        values = [
            "(%d,'c%d','%s','[%s]')" % (
                row_id, row_id % 8, body,
                ",".join(str((row_id * (i + 1)) % 100) for i in range(dim)),
            )
            for row_id in range(rows)
        ]
        for first in range(0, rows, batch):
            ask("INSERT INTO t VALUES "
                + ",".join(values[first:first + batch]) + ";")
        ask("CREATE VECTOR INDEX idx_t ON t(v);")

        query = (
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
        )
        times = []
        for _ in range(3):
            started = time.monotonic()
            ask(query, "ann")
            times.append(time.monotonic() - started)
        result["ann_s"] = round(min(times), 3)
        proc.stdin.close()
        proc.wait(timeout=120)

        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            log = handle.read()
        result["decodes"] = [
            {"scalar_decodes": int(m.group(1)), "hits": int(m.group(2)),
             "vector_decodes": int(m.group(3))}
            for m in DECODE_RE.finditer(log)
        ]
        result["scans"] = [
            {"cells": int(m.group(1)), "rows_measured": int(m.group(2)),
             "max_radius": float(m.group(3)), "radius_sum": float(m.group(4)),
             "largest_cell": int(m.group(5))}
            for m in STATS_RE.finditer(log)
        ]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--rows", type=int, default=400000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    report = {"args": vars(args), "runs": []}
    for label, binary in (("A", args.a), ("B", args.b)):
        result = run(binary, args.rows, args.dim, args.batch, args.text)
        result["label"] = label
        report["runs"].append(result)
        print("=== %s: %s ===" % (label, binary))
        print("  ann query          %8.3fs" % result["ann_s"])
        for scan, decode in zip(result["scans"], result["decodes"]):
            hit_rate = (
                decode["hits"] / max(decode["hits"] + decode["scalar_decodes"], 1)
            )
            print(
                "  -> cells=%-3d rows=%-8d scalar_decodes=%-7d hits=%-8d "
                "(hit %.1f%%) vector_decodes=%-7d"
                % (
                    scan["cells"], scan["rows_measured"],
                    decode["scalar_decodes"], decode["hits"],
                    hit_rate * 100, decode["vector_decodes"],
                )
            )

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
