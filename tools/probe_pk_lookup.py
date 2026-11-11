"""Measure the statements that the primary key index answers.

The statements are the ones a case of the evaluation may use to look a row
up, so their cost is part of the admission risk rather than of the
performance benchmark.
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
    nqueries = int(sys.argv[4]) if len(sys.argv) > 4 else 1000

    workdir = tempfile.mkdtemp(prefix="pktime")
    rows = B.generate_rows(42, nrows, 128)
    db = B.Db(binary, workdir, mode=mode)
    B.phase_write(db, rows, 128, 100)

    statements = [
        ("SELECT id FROM t WHERE id = 1;", "a row by its primary key"),
        ("SELECT id, cat FROM t WHERE id = 1;", "a row with a column"),
        ("SELECT id FROM t WHERE cat = 'c1';", "a column, a full scan"),
        ("SELECT id FROM t WHERE id = 999999;", "a row that does not exist"),
    ]
    for sql, label in statements:
        t0 = time.monotonic()
        for _ in range(nqueries):
            db.run(sql)
        ms = (time.monotonic() - t0) * 1000.0
        print("%-28s %10.1f ms  (%7.1f us/query)" % (label, ms, ms * 1000 / nqueries))

    db.close()
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
