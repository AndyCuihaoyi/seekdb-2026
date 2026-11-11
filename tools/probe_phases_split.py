"""Run the query phase with the built-in phase profiler, with or without a filter.

The two kinds of query do a different amount of work, so the phase timings
have to be looked at apart.
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
    nrows = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    nqueries = int(sys.argv[4]) if len(sys.argv) > 4 else 1000
    filtered = (sys.argv[5] if len(sys.argv) > 5 else "both")

    workdir = tempfile.mkdtemp(prefix="phaseq")
    rows = B.generate_rows(42, nrows, 128)
    queries = B.make_queries(7, nqueries, 128)
    lits = [B.vector_literal(q) for q in queries]

    lines = []
    for i in range(nqueries):
        use_filter = (filtered == "both" and i % 2 == 0) or filtered == "filtered"
        clause = " WHERE cat = 'c%d'" % (i % 8) if use_filter else ""
        sql = ("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
               "ORDER BY distance LIMIT 10;" % (lits[i], clause))
        lines.append(
            '{"id": %d, "op": "execute", "sql": "%s", "search_mode": "%s"}'
            % (i, sql.replace("\\", "\\\\").replace('"', '\\"'), mode)
        )
    lines.append('{"id": 99999, "op": "shutdown"}')

    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = workdir
    env["MINIVECDB_MODE"] = mode
    env["MINIVECDB_PROFILE"] = "1"

    loader = ['{"id": 0, "op": "execute", "sql": "CREATE TABLE t (id BIGINT '
              'PRIMARY KEY, cat VARCHAR(8), v VECTOR(128));", "search_mode": "%s"}' % mode]
    for batch in range(0, nrows, 100):
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
    load = subprocess.run([binary], input="\n".join(loader) + "\n",
                          capture_output=True, text=True, env=env)
    if load.returncode != 0:
        sys.stderr.write(load.stderr[-2000:])
        sys.exit("the load failed")

    proc = subprocess.run([binary], input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    sys.stderr.write(proc.stderr)
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
