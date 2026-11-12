#!/usr/bin/env python3
"""Report the memory of the structures that grow with the number of rows.

The requirement of the competition says that the database has to cope with a
data set that is *larger* than the memory budget, so every structure whose
size grows with the number of rows is a candidate for the out of memory
killer. This script measures the process at several moments of the life of a
table, so that the structures that grow are visible and can be told apart
from the ones that do not.

The measurements come from the process itself, read out of
`/proc/<pid>/status`, so they are the memory of the database and not of the
measurement.

Usage:
    tools/check_growing_structures.py [--binary PATH] [--rows N] [--dim N]

The script prints a table of the measured points and the growth between them.
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
        with open("/proc/%d/status" % handle_pid(pid)) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def handle_pid(pid):
    return pid


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


class Db:
    def __init__(self, binary, data_dir):
        self.proc = subprocess.Popen(
            [binary, "--data-dir", data_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.proc.stdout.readline()
        self.watcher = Watcher(self.proc.pid)
        self.watcher.start()

    def execute(self, sql, mode="exact"):
        request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the database exited: %s" % self.proc.stderr.read()[-2000:])
        return json.loads(line)

    def take_peak(self):
        value = self.watcher.peak
        self.watcher.peak = 0
        return value // 1024

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
        self.watcher.stop.set()
        self.watcher.join(timeout=5)


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

    work = tempfile.mkdtemp(prefix="mvdb-growing-")
    data_dir = os.path.join(work, "data")
    report = {}
    try:
        db = Db(args.binary, data_dir)
        try:
            db.execute(
                "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % args.dim
            )
            batch = 500
            for first in range(0, args.rows, batch):
                values = []
                for row_id in range(first, min(first + batch, args.rows)):
                    vec = "[" + ",".join(str((row_id * (i + 1)) % 100) for i in range(args.dim)) + "]"
                    values.append("(%d,'c%d','%s')" % (row_id, row_id % 8, vec))
                db.execute("INSERT INTO t VALUES " + ",".join(values) + ";")
            report["after_write"] = db.take_peak()

            db.execute("CREATE VECTOR INDEX idx_t ON t(v);")
            report["after_index_build"] = db.take_peak()

            # A statement that reads one row. It should not cost anything
            # that grows with the size of the table.
            db.execute("SELECT id FROM t LIMIT 1;")
            report["after_limit_1"] = db.take_peak()

            # A statement that filters. The inverted index answers it.
            db.execute("SELECT id FROM t WHERE cat = 'c3';")
            report["after_filtered"] = db.take_peak()

            # A statement that reads the whole table.
            db.execute("SELECT id FROM t;")
            report["after_full_scan"] = db.take_peak()

            # A search through the vector index.
            db.execute(
                "SELECT id, L2_DISTANCE(v, '[1,2,3,4]') AS d FROM t ORDER BY d LIMIT 10;"
            )
            report["after_search"] = db.take_peak()
        finally:
            db.close()

        # The restart is a process of its own: the recovery and the rebuild
        # of the indexes happen before it answers.
        db2 = Db(args.binary, data_dir)
        try:
            db2.execute("SELECT id FROM t LIMIT 1;")
            report["after_restart"] = db2.take_peak()
        finally:
            db2.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    order = [
        "after_write",
        "after_index_build",
        "after_limit_1",
        "after_filtered",
        "after_full_scan",
        "after_search",
        "after_restart",
    ]
    print("rows: %d, dim: %d" % (args.rows, args.dim))
    print("%-22s %10s" % ("point", "peak MiB"))
    for key in order:
        if key in report:
            print("%-22s %10d" % (key.replace("_", " "), report[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
