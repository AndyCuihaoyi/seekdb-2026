"""Measure the round trip latency of the protocol itself.

The benchmark measures the wall clock of the round trip, so the latency of
the pipe is part of the local number but not of the platform's number. The
latency is measured with a statement that does almost no work, so it is the
floor of every round trip.
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

    workdir = tempfile.mkdtemp(prefix="lat")
    rows = B.generate_rows(42, nrows, 128)
    db = B.Db(binary, workdir, mode=mode)
    B.phase_write(db, rows, 128, 100)

    # The latency floor: a statement that does not touch the table at all.
    # A failing statement is used on purpose: it is parsed and rejected
    # without a single row being read.
    t0 = time.monotonic()
    for _ in range(nqueries):
        try:
            db.run("SELECT 1;")
        except Exception:
            pass
    floor = (time.monotonic() - t0) * 1000.0

    # The same, without the JSON handling of the driver: the request is
    # written as a literal string and the answer is only counted.
    proc = subprocess.Popen(
        [binary, "--data-dir", workdir],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
        env={**os.environ, "MINIVECDB_DATA_DIR": workdir, "MINIVECDB_MODE": mode},
    )
    proc.stdout.readline()
    line = json.dumps({"id": 1, "op": "execute",
                       "sql": "SELECT 1;",
                       "search_mode": mode}) + "\n"
    t0 = time.monotonic()
    for _ in range(nqueries):
        proc.stdin.write(line)
        proc.stdin.flush()
        proc.stdout.readline()
    floor_raw = (time.monotonic() - t0) * 1000.0
    proc.stdin.write('{"id": 99999, "op": "shutdown"}\n')
    proc.stdin.flush()
    proc.stdout.readline()
    proc.wait()

    print("round trip, trivial statement, full driver : %8.1f ms  (%.1f us/query)"
          % (floor, floor * 1000 / nqueries))
    print("round trip, trivial statement, literal line : %8.1f ms  (%.1f us/query)"
          % (floor_raw, floor_raw * 1000 / nqueries))
    print()
    print("=> the protocol floor is about %.1f us per query" % (floor_raw * 1000 / nqueries))
    print("=> over 1000 queries that is %.1f ms of the measured total" % (floor_raw,))

    db.close()
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
