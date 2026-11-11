"""Profile only the fixed per-query cost.

The table holds a hundred rows, so the scan is negligible and the samples
attribute the parse, the literal handling, the rendering and the protocol.
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
    nrows = int(sys.argv[3]) if len(sys.argv) > 3 else 100
    nqueries = int(sys.argv[4]) if len(sys.argv) > 4 else 3000
    workdir = tempfile.mkdtemp(prefix="fixedq")
    perf_out = sys.argv[5] if len(sys.argv) > 5 else "/tmp/perf_fixed.data"

    rows = B.generate_rows(42, nrows, 128)
    queries = B.make_queries(7, nqueries, 128)
    db = B.Db(binary, workdir, mode=mode)
    B.phase_write(db, rows, 128, 100)

    lits = [B.vector_literal(q) for q in queries]
    lines = []
    for i in range(nqueries):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        sql = (
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
            "ORDER BY distance LIMIT 10;" % (lits[i], clause)
        )
        lines.append(
            '{"id": %d, "op": "execute", "sql": "%s", "search_mode": "%s"}'
            % (i, sql.replace("\\", "\\\\").replace('"', '\\"'), mode)
        )
    lines.append('{"id": 99999, "op": "shutdown"}')

    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = workdir
    env["MINIVECDB_MODE"] = mode
    cmd = ["perf", "record", "-F", "999", "-o", perf_out, "--", binary]
    proc = subprocess.run(cmd, input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    sys.stderr.write(proc.stderr[-1500:])
    print("perf data:", perf_out)
    db.close()
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
