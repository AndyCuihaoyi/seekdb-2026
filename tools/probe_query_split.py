"""Measure the query time of the filtered and the unfiltered queries apart.

The two kinds of query do a different amount of work: the unfiltered one
measures every row of the table, the filtered one measures the rows of the
inverted list of the filter column. The scan reads the whole block of
vectors either way, so it is worth knowing how the time is shared.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench as B


def main():
    binary = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    rows_n = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    queries_n = int(sys.argv[4]) if len(sys.argv) > 4 else 500

    rows = B.generate_rows(42, rows_n, 128)
    queries = B.make_queries(7, queries_n, 128)

    db = B.Db(binary, "/tmp/probe_split_data", mode=mode)
    B.phase_write(db, rows, 128, 100)
    lits = [B.vector_literal(q) for q in queries]

    def run(i, filtered):
        clause = " WHERE cat = 'c%d'" % (i % 8) if filtered else ""
        sql = ("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
               "ORDER BY distance LIMIT 10;" % (lits[i], clause))
        db.run(sql)

    for name, filtered in (("unfiltered", False), ("filtered  ", True)):
        # The warm up is not measured.
        for i in range(20):
            run(i, filtered)
        t0 = time.monotonic()
        for i in range(queries_n):
            run(i, filtered)
        dt = (time.monotonic() - t0) * 1000.0
        print("%s : %8.1f ms  (%.3f ms/query)" % (name, dt, dt / queries_n))

    db.close()


if __name__ == "__main__":
    main()
