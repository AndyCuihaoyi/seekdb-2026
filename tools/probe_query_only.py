"""Driver that runs only the query phase, so perf can attribute the query time.

Usage: python3 tools/probe_query_only.py <binary> <mode> <out_dir>
It loads the data set, then runs the 1000 TopK queries, printing the
timing of each query to stderr so the phases can be compared.
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench as B


def main():
    binary = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    workdir = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="probe")

    rows = B.generate_rows(42, 10000, 128)
    queries = B.make_queries(7, 1000, 128)

    db = B.Db(binary, workdir, mode=mode)
    B.phase_write(db, rows, 128, 100)

    # Warm up: one pass of every query, so that the blocks are in the cache.
    warm = [B.vector_literal(q) for q in queries]

    def one(i, filtered):
        clause = " WHERE cat = 'c%d'" % (i % 8) if filtered else ""
        sql = (
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
            "ORDER BY distance LIMIT 10;" % (warm[i], clause)
        )
        return db.run(sql)

    # Warm-up pass.
    for i in range(1000):
        one(i, i % 2 == 0)

    # Timed pass.
    t0 = time.monotonic()
    for i in range(1000):
        one(i, i % 2 == 0)
    total = (time.monotonic() - t0) * 1000.0

    # Timed pass, unfiltered only.
    t0 = time.monotonic()
    for i in range(1000):
        one(i, False)
    unfiltered = (time.monotonic() - t0) * 1000.0

    # Timed pass, filtered only.
    t0 = time.monotonic()
    for i in range(1000):
        one(i, True)
    filtered = (time.monotonic() - t0) * 1000.0

    # Timed pass with a trivial statement: the protocol + parse + JSON cost.
    # The dialect has no `SELECT 1`, so the cheapest statement that returns a
    # row is a primary key lookup.
    t0 = time.monotonic()
    for i in range(1000):
        db.run("SELECT id FROM t WHERE id = 1;")
    trivial = (time.monotonic() - t0) * 1000.0

    # Timed pass with the vector literal in the projection but a primary key
    # filter: the literal is parsed and one row is measured, so this is the
    # protocol + parse + literal-parse cost, without the scan.
    t0 = time.monotonic()
    for i in range(1000):
        db.run(
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t WHERE id = 1;"
            % warm[i]
        )
    literal = (time.monotonic() - t0) * 1000.0

    # Timed pass with a full scan and a filter but no distance: the cost of
    # the row walk and the predicate evaluation.
    t0 = time.monotonic()
    for i in range(1000):
        db.run("SELECT id FROM t WHERE cat = 'c1';")
    scanless = (time.monotonic() - t0) * 1000.0

    print(json.dumps({
        "mixed_1000": total,
        "unfiltered_1000": unfiltered,
        "filtered_1000": filtered,
        "pk_lookup_1000": trivial,
        "literal_parse_1000": literal,
        "scanless_filtered_1000": scanless,
    }, indent=2))

    db.close()
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
