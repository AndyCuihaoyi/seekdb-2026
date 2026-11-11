"""Profile the unfiltered TopK query alone.

The unfiltered query measures every row of the table, so it is the one that
shows what a row costs. The filtered one measures a small part of the table
and is dominated by the same work at a lower rate.

Usage: python3 tools/perf_query_unfiltered.py <binary> <mode> <out_dir> <perf-out>
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
    workdir = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="perfqu")
    perf_out = sys.argv[4] if len(sys.argv) > 4 else "/tmp/perf_qu.data"

    rows = B.generate_rows(42, 10000, 128)
    queries = B.make_queries(7, 1000, 128)
    lits = [B.vector_literal(q) for q in queries]

    loader = ['{"id": 0, "op": "execute", "sql": "CREATE TABLE t (id BIGINT '
              'PRIMARY KEY, cat VARCHAR(8), v VECTOR(128));", "search_mode": "%s"}' % mode]
    for batch in range(0, 10000, 100):
        chunk = rows[batch:batch + 100]
        values = ",".join(
            "(%d,'%s','%s')" % (pk, cat, B.vector_literal(vec))
            for pk, cat, vec in chunk
        )
        for stmt in ("BEGIN;", "INSERT INTO t VALUES " + values + ";", "COMMIT;"):
            loader.append(
                '{"id": 0, "op": "execute", "sql": "%s", "search_mode": "%s"}'
                % (stmt.replace('"', '\\"'), mode)
            )

    lines = []
    for i in range(1000):
        sql = ("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t "
               "ORDER BY distance LIMIT 10;" % lits[i])
        lines.append(
            '{"id": %d, "op": "execute", "sql": "%s", "search_mode": "%s"}'
            % (i, sql.replace("\\", "\\\\").replace('"', '\\"'), mode)
        )
    lines.append('{"id": 99999, "op": "shutdown"}')

    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = workdir
    env["MINIVECDB_MODE"] = mode

    load = subprocess.run([binary], input="\n".join(loader) + "\n",
                          capture_output=True, text=True, env=env)
    if load.returncode != 0:
        sys.stderr.write(load.stderr[-2000:])
        sys.exit("the load failed")

    cmd = ["perf", "record", "-F", "999", "-o", perf_out, "--", binary]
    proc = subprocess.run(cmd, input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    sys.stderr.write(proc.stderr[-1500:])
    print("perf data:", perf_out)
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
