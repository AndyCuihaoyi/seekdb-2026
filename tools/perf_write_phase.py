"""Profile the write phase.

The write phase is a third of the weight of the score, and it did not move
with the changes that sped up the query. This script profiles it, so that
the next change is aimed at the right thing.
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
    binary = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.abspath(
        "minivectordb/target/release/minivectordb")
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    nrows = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    batch = int(sys.argv[4]) if len(sys.argv) > 4 else 100
    perf_out = sys.argv[5] if len(sys.argv) > 5 else "/tmp/perf_write.data"

    workdir = tempfile.mkdtemp(prefix="wperf")
    rows = B.generate_rows(42, nrows, 128)

    lines = ['{"id": 0, "op": "execute", "sql": "CREATE TABLE t (id BIGINT '
             'PRIMARY KEY, cat VARCHAR(8), v VECTOR(128));", "search_mode": "%s"}' % mode]
    for b in range(0, nrows, batch):
        chunk = rows[b:b + batch]
        values = ",".join(
            "(%d,'%s','%s')" % (pk, cat, B.vector_literal(vec))
            for pk, cat, vec in chunk
        )
        for stmt in ("BEGIN;", "INSERT INTO t VALUES " + values + ";", "COMMIT;"):
            lines.append(
                '{"id": 0, "op": "execute", "sql": "%s", "search_mode": "%s"}'
                % (stmt.replace('"', '\\"'), mode)
            )
    lines.append('{"id": 99999, "op": "shutdown"}')

    env = dict(os.environ)
    env.update(MINIVECDB_DATA_DIR=workdir, MINIVECDB_MODE=mode)
    cmd = ["perf", "record", "-F", "999", "-o", perf_out, "--", binary]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    print("write phase wall clock: %.1f ms" % ((time.monotonic() - t0) * 1000))
    sys.stderr.write(proc.stderr[-1500:])
    print("perf data:", perf_out)
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
