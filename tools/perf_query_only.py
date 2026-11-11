"""Run only the query phase under `perf record`, so the samples attribute the
query time and nothing else.

Usage: python3 tools/perf_query_only.py <binary> <mode> <out_dir> <perf-out>
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench as B


def main():
    binary = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    workdir = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="perfq")
    perf_out = sys.argv[4] if len(sys.argv) > 4 else "/tmp/perf.data"

    rows = B.generate_rows(42, 10000, 128)
    queries = B.make_queries(7, 1000, 128)

    db = B.Db(binary, workdir, mode=mode)
    B.phase_write(db, rows, 128, 100)

    warm = [B.vector_literal(q) for q in queries]

    def one(i, filtered):
        clause = " WHERE cat = 'c%d'" % (i % 8) if filtered else ""
        sql = (
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
            "ORDER BY distance LIMIT 10;" % (warm[i], clause)
        )
        return db.run(sql)

    # Warm up: get every block into the cache, so that the samples attribute
    # the compute and not the file reads.
    for i in range(1000):
        one(i, i % 2 == 0)

    # Record the timed pass.
    cmd = [
        "perf", "record", "-F", "999", "-g", "--call-graph=dwarf,4096",
        "-o", perf_out, "--", binary,
    ]
    # The database is driven over stdin, so the query statements are piped in.
    lines = []
    for i in range(1000):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        lines.append(
            '{"id": %d, "op": "execute", "sql": "%s", "search_mode": "%s"}'
            % (i, sql_escape(
                "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
                "ORDER BY distance LIMIT 10;" % (warm[i], clause)), mode)
        )
    lines.append('{"id": 99999, "op": "shutdown"}')

    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = workdir
    env["MINIVECDB_MODE"] = mode
    proc = subprocess.run(cmd, input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    sys.stderr.write(proc.stderr[-3000:])
    print("perf data written to", perf_out)

    db.close()
    shutil.rmtree(workdir, ignore_errors=True)


def sql_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


if __name__ == "__main__":
    main()
