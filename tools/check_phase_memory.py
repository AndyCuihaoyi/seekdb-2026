#!/usr/bin/env python3
"""Measure the peak memory of every phase that can scale with the data set.

The requirement of the competition says that the database has to handle a
data set that is *larger* than the memory budget, so every phase that
walks the whole table has to keep a bounded memory. This script drives
the database through the phases that walk the whole table and reports the
peak memory of each one, so that a phase that grows with the data set is
visible instead of hidden.

The phases are:

* the write of the rows;
* the build of the vector index;
* a plain `SELECT` over the table;
* an `UPDATE` that touches one row;
* a `DELETE` that touches one row;
* the restart, which replays the log and rebuilds the indexes.

Usage:
    tools/check_phase_memory.py [--binary PATH] [--rows N] [--dim N]
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


def peak_rss_kb(pid):
    """The current resident set size of a process, in kilobytes.

    The *current* size is read rather than the high water mark, because the
    high water mark never goes down: a phase after the biggest one would
    report the biggest one, and the phases would not be told apart.
    """
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
            self.peak = max(self.peak, peak_rss_kb(self.pid))
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
        ready = self.proc.stdout.readline()
        if not ready:
            raise RuntimeError("the database closed its output: %s" % self.proc.stderr.read()[-2000:])
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

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
        self.watcher.stop.set()
        self.watcher.join(timeout=5)

    def take_peak(self):
        """The peak memory since the last call, and reset the mark."""
        value = self.watcher.peak
        self.watcher.peak = 0
        return value

def vector_literal(dim, seed):
    return "[" + ",".join(str((seed * (i + 1)) % 100) for i in range(dim)) + "]"


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

    work = tempfile.mkdtemp(prefix="mvdb-phase-")
    data_dir = os.path.join(work, "data")
    report = {"rows": args.rows, "dim": args.dim}
    try:
        db = Db(args.binary, data_dir)
        try:
            db.execute("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % args.dim)
            batch = 500
            for first in range(0, args.rows, batch):
                values = []
                for row_id in range(first, min(first + batch, args.rows)):
                    values.append(
                        "(%d,'c%d','%s')"
                        % (row_id, row_id % 8, vector_literal(args.dim, row_id))
                    )
                db.execute("INSERT INTO t VALUES " + ",".join(values) + ";")
            report["write_mib"] = db.take_peak() // 1024

            db.execute("CREATE VECTOR INDEX idx_t ON t(v);")
            report["index_build_mib"] = db.take_peak() // 1024

            db.execute("SELECT id FROM t WHERE cat = 'c3';")
            report["select_filtered_mib"] = db.take_peak() // 1024

            db.execute("SELECT id FROM t;")
            report["select_all_mib"] = db.take_peak() // 1024

            db.execute("UPDATE t SET cat = 'c9' WHERE id = 5;")
            report["update_one_mib"] = db.take_peak() // 1024

            db.execute("DELETE FROM t WHERE id = 6;")
            report["delete_one_mib"] = db.take_peak() // 1024

            db.execute("SELECT id, L2_DISTANCE(v, '%s') AS d FROM t ORDER BY d LIMIT 10;" % vector_literal(args.dim, 1))
            report["topk_mib"] = db.take_peak() // 1024
        finally:
            db.close()

        # The restart replays the log and rebuilds the indexes, so it is the
        # phase that has to fit in the budget as well.
        db2 = Db(args.binary, data_dir)
        try:
            db2.execute("SELECT id FROM t LIMIT 1;")
            report["restart_mib"] = db2.take_peak() // 1024
        finally:
            db2.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("%-22s %10s" % ("phase", "peak MiB"))
    for key in (
        "write_mib",
        "index_build_mib",
        "select_filtered_mib",
        "select_all_mib",
        "update_one_mib",
        "delete_one_mib",
        "topk_mib",
        "restart_mib",
    ):
        if key in report:
            print("%-22s %10d" % (key.replace("_mib", ""), report[key]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
