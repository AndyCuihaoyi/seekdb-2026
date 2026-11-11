"""Break the query phase into its parts, so the wall clock can be attributed.

The script sends the same TopK queries as the benchmark and compares the
round trip time of the TopK query with the round trip time of a trivial
statement, so that the protocol and the driver overhead can be separated from
the work inside the database.

Usage: python3 tools/probe_breakdown.py <binary> <mode> [rows] [queries]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench as B


def timed(db, fn, n):
    t0 = time.monotonic()
    for i in range(n):
        fn(i)
    return (time.monotonic() - t0) * 1000.0


def main():
    binary = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "ann"
    rows_n = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    queries_n = int(sys.argv[4]) if len(sys.argv) > 4 else 1000

    rows = B.generate_rows(42, rows_n, 128)
    queries = B.make_queries(7, queries_n, 128)

    db = B.Db(binary, "/tmp/probe_breakdown_data", mode=mode)
    B.phase_write(db, rows, 128, 100)
    db.run("CREATE VECTOR INDEX idx_t ON t(v);")
    lits = [B.vector_literal(q) for q in queries]

    # A trivial statement: the cost of the protocol and of the driver alone.
    trivial = timed(db, lambda i: db.run("SELECT id FROM t LIMIT 1;"), queries_n)
    print("trivial round trip : %8.1f ms  (%.3f ms/query)" % (trivial, trivial / queries_n))

    def topk(i):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        sql = ("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
               "ORDER BY distance LIMIT 10;" % (lits[i], clause))
        db.run(sql)

    topk_ms = timed(db, topk, queries_n)
    print("topk round trip    : %8.1f ms  (%.3f ms/query)" % (topk_ms, topk_ms / queries_n))

    # A query with the same statement but a tiny literal: the parsing of the
    # 128 dimension literal is separated from the scan.
    tiny_lit = "[" + ",".join("0.5" for _ in range(128)) + "]"

    def tiny(i):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        sql = ("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
               "ORDER BY distance LIMIT 10;" % (tiny_lit, clause))
        db.run(sql)

    tiny_ms = timed(db, tiny, queries_n)
    print("tiny literal       : %8.1f ms  (%.3f ms/query)" % (tiny_ms, tiny_ms / queries_n))

    # The same statement without the vector literal at all.
    def plain(i):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        sql = "SELECT id FROM t%s LIMIT 10;" % clause
        db.run(sql)

    plain_ms = timed(db, plain, queries_n)
    print("plain select       : %8.1f ms  (%.3f ms/query)" % (plain_ms, plain_ms / queries_n))

    db.close()


if __name__ == "__main__":
    main()
