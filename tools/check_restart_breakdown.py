#!/usr/bin/env python3
"""Report the memory of a restart, broken down by kind.

`VmRSS` says how much the process holds, but not *what* it holds. The
judge counts the whole container, so it matters whether the memory is
anonymous (the heap of the process) or the page cache of the files it
read. This script samples both while the database recovers, so that a
growth can be attributed.

Usage:
    tools/check_restart_breakdown.py [--binary PATH] [--rows N] [--dim N]
                                      [--text N]
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


def sample(pid):
    """The memory of the process, in kilobytes."""
    out = {"rss": 0, "anon": 0, "file": 0}
    try:
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["rss"] = int(line.split()[1])
    except OSError:
        return out
    try:
        with open("/proc/%d/smaps_rollup" % pid) as f:
            for line in f:
                if line.startswith("Anonymous:"):
                    out["anon"] = int(line.split()[1])
                elif line.startswith("Private_Dirty:"):
                    out["file"] = out.get("file", 0)
    except OSError:
        pass
    return out


class Watcher(threading.Thread):
    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak = {"rss": 0, "anon": 0}
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            s = sample(self.pid)
            self.peak["rss"] = max(self.peak["rss"], s["rss"])
            self.peak["anon"] = max(self.peak["anon"], s["anon"])
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
        raise RuntimeError("the database exited")
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
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-breakdown-")
    data_dir = os.path.join(work, "data")
    body = "x" * args.text if args.text else ""
    out = {"rows": args.rows, "text": args.text}
    try:
        proc = start(args.binary, data_dir)
        try:
            if args.text:
                ask(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                          "body VARCHAR(1024), v VECTOR(%d));" % args.dim)
            else:
                ask(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                          "v VECTOR(%d));" % args.dim)
            for first in range(0, args.rows, 200):
                values = []
                for row_id in range(first, min(first + 200, args.rows)):
                    vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(args.dim))
                    if args.text:
                        values.append("(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec))
                    else:
                        values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
                ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
        finally:
            proc.stdin.close()
            proc.wait(timeout=180)

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
        out["peak_rss_mib"] = watcher.peak["rss"] // 1024
        out["peak_anon_mib"] = watcher.peak["anon"] // 1024
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
