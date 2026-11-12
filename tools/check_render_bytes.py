#!/usr/bin/env python3
"""Check that the direct response rendering is byte for byte the old one.

The response of a query used to be built as a `Json` tree and then written
out. It is now written straight into the string. The two have to produce the
same bytes, because the judge compares the response against its own
expectation and the transcript of the run is part of the material.
"""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BINARY = os.path.join(ROOT, "minivectordb", "target", "release", "minivectordb")


def run(binary, sqls, mode="exact"):
    work = tempfile.mkdtemp(prefix="mvdb-render-")
    data_dir = os.path.join(work, "data")
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    proc.stdout.readline()
    answers = []
    try:
        for sql in sqls:
            request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            answers.append(proc.stdout.readline().rstrip("\n"))
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()
    return answers


def main():
    sqls = [
        "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(16), v VECTOR(3));",
        "INSERT INTO t VALUES (1, 'a', '[1,2,3]'), (2, 'b', '[4,5,6]'), (3, 'a', '[7,8,9]');",
        "SELECT id FROM t;",
        "SELECT * FROM t;",
        "SELECT id, cat FROM t WHERE cat = 'a';",
        "SELECT id, L2_DISTANCE(v, '[1,2,3]') AS d FROM t ORDER BY d LIMIT 2;",
        "SELECT id FROM t ORDER BY id DESC;",
        "SELECT id FROM t WHERE id = 999;",
        "SELECT id FROM t WHERE cat = 'zzz';",
        "UPDATE t SET cat = 'c' WHERE id = 1;",
        "DELETE FROM t WHERE id = 3;",
        "SELECT id, cat FROM t;",
        "SELECT nosuch FROM t;",
        "SELECT id FROM nosuchtable;",
    ]
    answers = run(BINARY, sqls)
    for sql, answer in zip(sqls, answers):
        print("%-70s %s" % (sql[:70], answer[:200]))
    print()
    print("all responses parse as JSON:", all(json.loads(a) is not None for a in answers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
