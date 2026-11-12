#!/usr/bin/env python3
"""Measure the memory of a start on a table that holds long text.

The judge runs the container with `--memory=2g --memory-swap=2g`. The
`ScalarBlock` of the block cache used to be accounted for at 64 bytes a
row, so a table that holds a long text was under-counted by a factor of
fifty and the cache held far more than its budget said. The startup
rebuild of the inverted indexes read every block of the table, so the
whole table landed in the cache and the out of memory killer took the
process before the handshake - which the judge reads as a database that
exited before it answered.

This script writes a table that holds a long text, restarts the database
on the same data directory and reports the peak memory of the restart.

Usage:
    tools/check_startup_text_memory.py [--binary PATH] [--rows N]
                                       [--text N] [--dim N]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time


class Watcher(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak = 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                with open("/proc/%d/status" % self.pid) as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            self.peak = max(self.peak, int(line.split()[1]))
                            break
            except OSError:
                pass
            time.sleep(0.02)


def start(binary, data_dir):
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError("the database closed its output")
    return proc


def ask(proc, sql):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database exited before the response")
    return json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "minivectordb",
            "target",
            "release",
            "minivectordb",
        ),
    )
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-text-")
    data_dir = os.path.join(work, "data")
    out = {"rows": args.rows, "text": args.text}
    try:
        proc = start(args.binary, data_dir)
        try:
            ask(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                      "body VARCHAR(1024), v VECTOR(%d));" % args.dim)
            body = "x" * args.text
            for first in range(0, args.rows, 200):
                values = []
                for row_id in range(first, min(first + 200, args.rows)):
                    values.append(
                        "(%d,'c%d','%s','[%s]')"
                        % (
                            row_id,
                            row_id % 8,
                            body,
                            ",".join(str((row_id * (i + 1)) % 100) for i in range(args.dim)),
                        )
                    )
                ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
        finally:
            proc.stdin.close()
            proc.wait(timeout=120)

        # The restart is the measurement: the rebuild of the inverted
        # indexes happens before the handshake.
        proc = start(args.binary, data_dir)
        watcher = Watcher(proc.pid)
        watcher.start()
        try:
            ask(proc, "SELECT id FROM t LIMIT 1;")
            time.sleep(0.2)
        finally:
            watcher.stop.set()
            watcher.join(timeout=5)
            proc.stdin.close()
            proc.wait(timeout=60)
        out["restart_peak_mib"] = watcher.peak // 1024
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
