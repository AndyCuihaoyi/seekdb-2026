#!/usr/bin/env python3
"""Compare two binaries on the same machine, in turn, so that the machine's
drift cannot be mistaken for a difference between the two.

The measurement of the machine is noisy: the same binary has been seen
between 735 ms and 991 ms for the query phase of the benchmark. A
comparison of two binaries that runs one after the other would therefore
measure the machine, not the code.

So the two binaries are run alternately, several times each, and the
median of each is reported. The medians are also reported with the
interleaving visible, so that a drift is visible in the output itself
rather than hidden inside an average.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bench as B  # noqa: E402


def one_round(binary, mode, rows, dim, queries, batch):
    """Run the benchmark once and return its measurements."""
    work = tempfile.mkdtemp(prefix="mvdb-cmp-")
    try:
        db = B.Db(binary, work, mode=mode)
        data = B.generate_rows(42, rows, dim)
        qs = B.make_queries(42, queries, dim)
        B.phase_write(db, data, dim, batch)
        db.run("CREATE VECTOR INDEX idx_t ON t(v);")
        cats = ["c%d" % i for i in range(B.CATEGORY_COUNT)]

        t0 = os.times()
        import time
        start = time.monotonic()
        answers, flags = B.phase_query(db, qs, 10, 0.5, cats)
        query_ms = (time.monotonic() - start) * 1000
        db.close()
        return {"query_t_ms": query_ms}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="the first binary")
    ap.add_argument("--b", required=True, help="the second binary")
    ap.add_argument("--mode", default="exact")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--queries", type=int, default=1000)
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args()

    a = os.path.abspath(args.a)
    b = os.path.abspath(args.b)

    sa, sb = [], []
    print("round      A (ms)      B (ms)")
    for i in range(args.rounds):
        ra = one_round(a, args.mode, args.rows, args.dim, args.queries, 100)
        rb = one_round(b, args.mode, args.rows, args.dim, args.queries, 100)
        sa.append(ra["query_t_ms"])
        sb.append(rb["query_t_ms"])
        print("%5d %11.1f %11.1f" % (i + 1, sa[-1], sb[-1]))

    ma, mb = statistics.median(sa), statistics.median(sb)
    print()
    print("A median %.1f ms   spread %.1f%%" % (ma, 100 * (max(sa) - min(sa)) / min(sa)))
    print("B median %.1f ms   spread %.1f%%" % (mb, 100 * (max(sb) - min(sb)) / min(sb)))
    print("B is %.3fx the time of A" % (mb / ma))


if __name__ == "__main__":
    sys.exit(main())
