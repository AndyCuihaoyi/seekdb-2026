#!/usr/bin/env python3
"""Find which part of the recovery holds the memory.

The restart of a large table costs more memory than the budget allows, and
the recovery has four steps that could be the cause:

* the replay of the log;
* the rebuild of the primary key index;
* the rebuild of the inverted indexes;
* the rebuild of the vector index.

This script runs the recovery with the memory of the process reported at
each step, by asking the database a question after each one. The steps
are told apart by the question, and the memory is read from the process
while it answers.

The trick is that the recovery of the *start* is what is measured, so the
database is started on a data directory that is already written.

Usage:
    tools/check_recovery_memory.py [--rows N] [--dim N]
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


def ask(proc, sql):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
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
    parser.add_argument("--dim", type=int, default=128)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-recovery-")
    data_dir = os.path.join(work, "data")
    try:
        proc = start(args.binary, data_dir)
        try:
            ask(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % args.dim)
            for first in range(0, args.rows, 500):
                values = []
                for row_id in range(first, min(first + 500, args.rows)):
                    values.append(
                        "(%d,'c%d','[%s]')"
                        % (
                            row_id,
                            row_id % 8,
                            ",".join(str((row_id * (i + 1)) % 100) for i in range(args.dim)),
                        )
                    )
                ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
        finally:
            close(proc)

        sizes = {}
        for root, _dirs, files in os.walk(data_dir):
            for name in files:
                path = os.path.join(root, name)
                sizes[os.path.relpath(path, data_dir)] = os.path.getsize(path)
        print("data directory:")
        for name, size in sorted(sizes.items()):
            print("  %-24s %10d" % (name, size))

        proc = start(args.binary, data_dir)
        watcher = Watcher(proc.pid)
        watcher.start()
        try:
            time.sleep(0.3)
            after_start = watcher.peak
            watcher.peak = 0
            ask(proc, "SELECT id FROM t LIMIT 1;")
            time.sleep(0.2)
            after_query = watcher.peak
        finally:
            watcher.stop.set()
            watcher.join(timeout=5)
            close(proc)
        print()
        print("%-28s %10d MiB" % ("after the handshake", after_start // 1024))
        print("%-28s %10d MiB" % ("after one query", after_query // 1024))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
