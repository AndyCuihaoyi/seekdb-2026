"""Measure the CPU time of the server during the query phase.

The wall clock of the query phase is compared with the CPU time the server
process actually used, so that a phase which waits for something else than
the CPU can be told apart from a phase that burns the CPU.

Usage: python3 tools/probe_cpu.py <binary> <mode> [rows] [queries]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench as B


def proc_cpu(pid):
    with open("/proc/%d/stat" % pid) as f:
        parts = f.read().rsplit(")", 1)[1].split()
    # fields after comm: state is parts[0], utime is 11, stime is 12
    return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")


def main():
    binary = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "ann"
    rows_n = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    queries_n = int(sys.argv[4]) if len(sys.argv) > 4 else 1000

    rows = B.generate_rows(42, rows_n, 128)
    queries = B.make_queries(7, queries_n, 128)

    db = B.Db(binary, "/tmp/probe_cpu_data", mode=mode)
    B.phase_write(db, rows, 128, 100)
    db.run("CREATE VECTOR INDEX idx_t ON t(v);")
    lits = [B.vector_literal(q) for q in queries]

    # Warm up, so that the caches are hot.
    for i in range(queries_n):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        db.run("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
               "ORDER BY distance LIMIT 10;" % (lits[i], clause))

    pid = db.proc.pid
    c0 = proc_cpu(pid)
    t0 = time.monotonic()
    for i in range(queries_n):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        db.run("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
               "ORDER BY distance LIMIT 10;" % (lits[i], clause))
    wall = (time.monotonic() - t0) * 1000.0
    c1 = proc_cpu(pid)
    print("wall   : %8.1f ms" % wall)
    print("cpu    : %8.1f ms  (%.0f%% of the wall clock)" % ((c1 - c0) * 1000, (c1 - c0) * 1000 / wall * 100))
    db.close()


if __name__ == "__main__":
    main()
