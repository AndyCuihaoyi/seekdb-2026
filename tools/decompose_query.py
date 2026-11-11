"""Decompose the query cost into a fixed per-query part and a per-row part.

The same TopK statement is run against tables of different sizes. The
intercept of the fit is the fixed cost of one query (protocol, parse of the
vector literal, rendering of the answer); the slope is the cost of one
scanned row.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench as B


def measure(binary, mode, nrows, nqueries, filtered):
    workdir = tempfile.mkdtemp(prefix="decomp")
    rows = B.generate_rows(42, nrows, 128)
    queries = B.make_queries(7, nqueries, 128)
    db = B.Db(binary, workdir, mode=mode)
    B.phase_write(db, rows, 128, 100)
    lits = [B.vector_literal(q) for q in queries]

    def one(i):
        clause = " WHERE cat = 'c%d'" % (i % 8) if filtered else ""
        sql = (
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
            "ORDER BY distance LIMIT 10;" % (lits[i], clause)
        )
        return db.run(sql)

    # Warm up.
    for i in range(nqueries):
        one(i)
    t0 = time.monotonic()
    for i in range(nqueries):
        one(i)
    ms = (time.monotonic() - t0) * 1000.0
    db.close()
    shutil.rmtree(workdir, ignore_errors=True)
    return ms


def main():
    binary = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    sizes = [100, 500, 1000, 2000, 5000, 10000]
    nqueries = 300
    out = {}
    for filtered in (False, True):
        label = "filtered" if filtered else "unfiltered"
        points = []
        for n in sizes:
            ms = measure(binary, mode, n, nqueries, filtered)
            per_q = ms / nqueries
            points.append((n, per_q))
            print(f"{label:11s} rows={n:6d}  {per_q*1000:8.1f} us/query")
        # Least squares fit: per_q = a + b * rows
        n = len(points)
        sx = sum(p[0] for p in points)
        sy = sum(p[1] for p in points)
        sxx = sum(p[0] * p[0] for p in points)
        sxy = sum(p[0] * p[1] for p in points)
        b = (n * sxy - sx * sy) / (n * sxx - sx * sx)
        a = (sy - b * sx) / n
        out[label] = {"fixed_us": a * 1000, "per_row_ns": b * 1e9}
        print(f"{label:11s} -> fixed {a*1000:.1f} us/query, {b*1e9:.1f} ns/row")
        print()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
