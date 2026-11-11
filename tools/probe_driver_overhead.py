"""Measure the overhead of the local driver itself.

The benchmark measures the wall clock of the round trip, so the time the
Python driver spends building the request and parsing the response is part
of the measurement. The platform measures the request handling on its side,
so the two numbers have to be told apart.
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


def main():
    binary = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    nrows = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    nqueries = int(sys.argv[4]) if len(sys.argv) > 4 else 1000

    workdir = tempfile.mkdtemp(prefix="ovh")
    rows = B.generate_rows(42, nrows, 128)
    queries = B.make_queries(7, nqueries, 128)
    lits = [B.vector_literal(q) for q in queries]

    db = B.Db(binary, workdir, mode=mode)
    B.phase_write(db, rows, 128, 100)

    # The full round trip, as the benchmark does it.
    t0 = time.monotonic()
    for i in range(nqueries):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        sql = ("SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
               "ORDER BY distance LIMIT 10;" % (lits[i], clause))
        db.run(sql)
    roundtrip = (time.monotonic() - t0) * 1000.0

    # The same statements, but the driver only writes and reads the lines,
    # without building or parsing any JSON.
    statements = []
    for i in range(nqueries):
        clause = " WHERE cat = 'c%d'" % (i % 8) if i % 2 == 0 else ""
        statements.append(
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
            "ORDER BY distance LIMIT 10;" % (lits[i], clause))

    proc = subprocess.Popen(
        [binary, "--data-dir", workdir],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
        env={**os.environ, "MINIVECDB_DATA_DIR": workdir, "MINIVECDB_MODE": mode},
    )
    proc.stdout.readline()
    t0 = time.monotonic()
    for i, sql in enumerate(statements):
        proc.stdin.write(sql.replace('"', '\\"') + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
    raw = (time.monotonic() - t0) * 1000.0
    proc.stdin.write('{"id": 99999, "op": "shutdown"}\n')
    proc.stdin.flush()
    proc.stdout.readline()
    proc.wait()

    # The cost of the JSON handling of the driver alone.
    t0 = time.monotonic()
    for i in range(nqueries):
        request = {"id": i, "op": "execute", "sql": statements[i], "search_mode": mode}
        json.dumps(request, ensure_ascii=False)
    build = (time.monotonic() - t0) * 1000.0

    sample = ('{"id":1,"ok":true,"columns":["id","distance"],"rows":['
              + ",".join('[1,"0.123456"]' for _ in range(10)) + "]}")
    t0 = time.monotonic()
    for _ in range(nqueries):
        json.loads(sample)
    parse = (time.monotonic() - t0) * 1000.0

    print("round trip through the driver : %8.1f ms" % roundtrip)
    print("raw statements, no JSON in the driver: %8.1f ms" % raw)
    print("driver JSON build cost        : %8.1f ms" % build)
    print("driver JSON parse cost        : %8.1f ms" % parse)
    print()
    print("=> the database did %8.1f ms of work" % (raw,))
    print("=> the driver overhead is %8.1f ms" % (roundtrip - raw))

    db.close()
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
