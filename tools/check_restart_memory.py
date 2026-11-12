#!/usr/bin/env python3
"""Measure the memory of the restart alone, at several sizes.

The statement requires that the database handles a data set that is larger
than the memory budget, and it names the *recovery* as one of the things
that has to cope with it. The restart is therefore measured on its own:
the data is written in one process, and a second process is started on the
same data directory and asked a single question.

The peak of the second process is the memory of the recovery: the replay
of the log, the rebuild of the primary key index, the rebuild of the
inverted indexes and the rebuild of the vector index.

Usage:
    tools/check_restart_memory.py [--rows N,N,N] [--dim N]
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


def rss_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


class Watcher(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak = 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            self.peak = max(self.peak, rss_kb(self.pid))
            time.sleep(0.02)


def ask(proc, sql, mode="exact"):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database exited")
    return json.loads(line)


def start(binary, data_dir):
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError("the database closed its output: %s" % proc.stderr.read()[-2000:])
    return proc


def close(proc):
    try:
        proc.stdin.close()
        proc.wait(timeout=30)
    except Exception:
        proc.kill()


def write_data(binary, data_dir, rows, dim, index):
    proc = start(binary, data_dir)
    try:
        ask(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim)
        for first in range(0, rows, 500):
            values = []
            for row_id in range(first, min(first + 500, rows)):
                values.append(
                    "(%d,'c%d','[%s]')"
                    % (
                        row_id,
                        row_id % 8,
                        ",".join(str((row_id * (i + 1)) % 100) for i in range(dim)),
                    )
                )
            ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
        if index:
            ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
    finally:
        close(proc)


def measure_restart(binary, data_dir):
    proc = start(binary, data_dir)
    watcher = Watcher(proc.pid)
    watcher.start()
    try:
        ask(proc, "SELECT id FROM t LIMIT 1;")
        time.sleep(0.2)
    finally:
        watcher.stop.set()
        watcher.join(timeout=5)
        close(proc)
    return watcher.peak // 1024


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
    parser.add_argument("--rows", default="50000,200000,800000")
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--no-index", action="store_true")
    args = parser.parse_args()

    print("%9s %14s %14s" % ("rows", "without index", "with index"))
    for rows in [int(r) for r in args.rows.split(",")]:
        work = tempfile.mkdtemp(prefix="mvdb-restart-")
        data_dir = os.path.join(work, "data")
        try:
            write_data(args.binary, data_dir, rows, args.dim, False)
            without = measure_restart(args.binary, data_dir)
            proc = start(args.binary, data_dir)
            try:
                ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            finally:
                close(proc)
            with_index = measure_restart(args.binary, data_dir)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print("%9d %11d MiB %11d MiB" % (rows, without, with_index))
    return 0


if __name__ == "__main__":
    sys.exit(main())
