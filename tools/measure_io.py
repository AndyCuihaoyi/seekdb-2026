#!/usr/bin/env python3
"""Measure the I/O of the two phases the way the platform measures it, and
the way our benchmark used to measure it, so that the two can be compared.

The platform reads `rbytes` and `wbytes` from `io.stat` of the cgroup of
the container. Those are the bytes that went to the block device: a read
that the page cache served is not one of them.

The old measurement of this repository used `rchar` and `wchar` of
`/proc/<pid>/io`. Those two count **every** byte the process read or
wrote, including the bytes that came in and went out through the pipes of
the protocol. The SQL text of the benchmark is 1.17 MB for the queries and
11.7 MB for the load, so the pipe traffic was a large part of the number
that was reported as the I/O of the phase.

This script reports all of them side by side:

* `rchar` / `wchar` — what the old measurement called the I/O;
* `read_bytes` / `write_bytes` — the bytes that reached the block device,
  which is the closest local stand-in for what the platform counts;
* `syscr` / `syscw` — the number of the calls, which is a measure of the
  work rather than of the volume.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bench as B  # noqa: E402


def read_io(pid):
    """Every counter of `/proc/<pid>/io` that matters here."""
    out = {}
    with open("/proc/%d/io" % pid) as f:
        for line in f:
            key, _, value = line.partition(":")
            try:
                out[key.strip()] = int(value)
            except ValueError:
                pass
    return out


def delta(before, after):
    return {k: after.get(k, 0) - before.get(k, 0) for k in after}


def report(name, d):
    mb = lambda v: v / 1048576.0
    print("%s" % name)
    print("  rchar        %9.3f MB   (includes the traffic of the pipe)" % mb(d["rchar"]))
    print("  wchar        %9.3f MB   (includes the traffic of the pipe)" % mb(d["wchar"]))
    print("  read_bytes   %9.3f MB   (reached the block device)" % mb(d["read_bytes"]))
    print("  write_bytes  %9.3f MB   (reached the block device)" % mb(d["write_bytes"]))
    print("  syscr        %9d calls" % d["syscr"])
    print("  syscw        %9d calls" % d["syscw"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=None)
    ap.add_argument("--mode", default="exact")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--queries", type=int, default=1000)
    ap.add_argument("--cpu", default=None)
    args = ap.parse_args()

    binary = args.binary or os.path.join(ROOT, "minivectordb", "target", "release", "minivectordb")
    work = tempfile.mkdtemp(prefix="mvdb-io-")
    try:
        db = B.Db(binary, work, mode=args.mode,
                  cpu=int(args.cpu) if args.cpu else None)
        rows = B.generate_rows(42, args.rows, args.dim)
        queries = B.make_queries(42, args.queries, args.dim)
        cats = ["c%d" % i for i in range(B.CATEGORY_COUNT)]
        pid = db.proc.pid

        before = read_io(pid)
        B.phase_write(db, rows, args.dim, 100)
        after_write = read_io(pid)
        db.run("CREATE VECTOR INDEX idx_t ON t(v);")
        after_index = read_io(pid)

        t0 = time.monotonic()
        B.phase_query(db, queries, 10, 0.5, cats)
        query_ms = (time.monotonic() - t0) * 1000
        after_query = read_io(pid)

        report("write phase:", delta(before, after_write))
        report("index build:", delta(after_write, after_index))
        report("query phase:", delta(after_index, after_query))
        print()
        print("  query phase: %.1f ms" % query_ms)
        db.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
