"""Measure the round trip time of a statement that touches no row.

The query phase is measured end to end, so the cost of the protocol and of
the driver is part of it. This script measures that cost alone, by sending a
statement that is answered without reading a row.
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
    queries_n = int(sys.argv[4]) if len(sys.argv) > 4 else 1000

    rows = B.generate_rows(42, rows_n, 128)
    queries = B.make_queries(7, queries_n, 128)

    db = B.Db(binary, "/tmp/probe_roundtrip_data", mode=mode)
    B.phase_write(db, rows, 128, 100)

    def timed(fn, n):
        t0 = time.monotonic()
        for i in range(n):
            fn(i)
        return (time.monotonic() - t0) * 1000.0

    # A lookup of a row that does not exist: the primary key index answers
    # it without reading the table.
    empty = timed(lambda i: db.run("SELECT id FROM t WHERE id = -1;"), queries_n)
    print("empty lookup   : %8.1f ms  (%.3f ms/query)" % (empty, empty / queries_n))

    # The same, but with the vector literal of a real query, so that the
    # parsing of the literal is part of the measurement.
    lits = [B.vector_literal(q) for q in queries]
    def with_literal(i):
        db.run("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t WHERE id = -1;"
               % lits[i])
    lit = timed(with_literal, queries_n)
    print("with literal   : %8.1f ms  (%.3f ms/query)" % (lit, lit / queries_n))

    db.close()


if __name__ == "__main__":
    main()
