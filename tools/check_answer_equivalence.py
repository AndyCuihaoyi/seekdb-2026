#!/usr/bin/env python3
"""Compare the answers of two builds of the database, statement by statement.

The changes of this round are meant to change the *memory* of the database
and not its answers. The two builds are therefore driven through the same
statements and their answers are compared byte for byte.

The statements cover the shapes that the round touched:

* a plain scan of a table, with and without a filter;
* a statement with a limit, with and without an ordering;
* an ordering by a column, by a distance and by the primary key;
* a statement that reads the vector column and one that does not;
* the rows a transaction changed;
* a restart in the middle of the sequence.

Usage:
    tools/check_answer_equivalence.py [--old PATH] [--new PATH] [--verbose]

The script exits with a non-zero status when an answer differs.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile


class Database:
    """One database process, driven over the standard protocol."""

    def __init__(self, binary, data_dir):
        self.binary = binary
        self.data_dir = data_dir
        self.proc = None
        self.start()

    def start(self):
        self.proc = subprocess.Popen(
            [self.binary, "--data-dir", self.data_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = self.proc.stdout.readline()
        if not ready:
            raise RuntimeError(
                "the database closed its output: %s" % self.proc.stderr.read()[-2000:]
            )

    def execute(self, sql, mode="exact"):
        request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(
                "the database exited: %s" % self.proc.stderr.read()[-2000:]
            )
        return line.rstrip("\n")

    def restart(self):
        self.close()
        self.start()

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=30)
        except Exception:
            self.proc.kill()
        self.proc = None


# The statements, in the order they are run. A `#restart` line restarts the
# database on the same data directory before the next statement.
STATEMENTS = [
    "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(16), num BIGINT, v VECTOR(3));",
    "CREATE TABLE u (id BIGINT PRIMARY KEY, name VARCHAR(32));",
    "INSERT INTO t VALUES (1, 'a', 10, '[1,0,0]'), (2, 'b', 20, '[0,1,0]'), "
    "(3, 'a', 30, '[0,0,1]'), (4, 'c', 40, '[1,1,1]'), (5, 'b', 50, '[2,0,0]');",
    "INSERT INTO u VALUES (7, 'seven');",
    "SELECT id FROM t;",
    "SELECT * FROM t;",
    "SELECT id, cat, num FROM t WHERE cat = 'a';",
    "SELECT id FROM t WHERE num >= 30;",
    "SELECT id FROM t WHERE cat = 'a' AND num > 10;",
    "SELECT id FROM t LIMIT 1;",
    "SELECT id FROM t LIMIT 3;",
    "SELECT id FROM t WHERE cat = 'b' LIMIT 2;",
    "SELECT id FROM t ORDER BY id DESC;",
    "SELECT id FROM t ORDER BY id DESC LIMIT 2;",
    "SELECT id, num FROM t ORDER BY num;",
    "SELECT id, num FROM t ORDER BY num DESC;",
    "SELECT id, L2_DISTANCE(v, '[1,0,0]') AS d FROM t ORDER BY d;",
    "SELECT id, L2_DISTANCE(v, '[1,0,0]') AS d FROM t ORDER BY d LIMIT 3;",
    "SELECT id, L2_DISTANCE(v, '[1,0,0]') AS d FROM t WHERE cat = 'b' ORDER BY d;",
    "SELECT id FROM t WHERE id = 3;",
    "SELECT id FROM t WHERE id = 99;",
    "SELECT id FROM t WHERE cat = 'zzz';",
    "SELECT id FROM u;",
    "SELECT id, cat FROM t WHERE cat = 'a' ORDER BY id;",
    # A restart in the middle: the answers have to survive it.
    "#restart",
    "SELECT id FROM t;",
    "SELECT * FROM t;",
    "SELECT id FROM t WHERE cat = 'a';",
    "SELECT id FROM t LIMIT 1;",
    "SELECT id, L2_DISTANCE(v, '[1,0,0]') AS d FROM t ORDER BY d;",
    "SELECT id FROM u;",
    # The changes of a transaction.
    "BEGIN;",
    "INSERT INTO t VALUES (6, 'd', 60, '[3,0,0]');",
    "UPDATE t SET cat = 'z' WHERE id = 1;",
    "DELETE FROM t WHERE id = 2;",
    "SELECT id, cat FROM t;",
    "SELECT id FROM t WHERE cat = 'z';",
    "SELECT id FROM t WHERE cat = 'd';",
    "SELECT id FROM t LIMIT 2;",
    "COMMIT;",
    "SELECT id, cat FROM t;",
    "SELECT id FROM t WHERE cat = 'z';",
    # A rollback leaves nothing behind.
    "BEGIN;",
    "INSERT INTO t VALUES (7, 'e', 70, '[4,0,0]');",
    "DELETE FROM t WHERE id = 3;",
    "ROLLBACK;",
    "SELECT id, cat FROM t;",
    "SELECT id FROM t WHERE cat = 'e';",
    # A vector index, in both modes.
    "CREATE VECTOR INDEX idx_t ON t(v);",
    "SELECT id, L2_DISTANCE(v, '[1,0,0]') AS d FROM t ORDER BY d LIMIT 3;",
    "#restart",
    "SELECT id FROM t;",
    "SELECT id, L2_DISTANCE(v, '[1,0,0]') AS d FROM t ORDER BY d LIMIT 3;",
]


def run(binary, statements):
    """The answers of one build, in the order of the statements."""
    work = tempfile.mkdtemp(prefix="mvdb-equiv-")
    data_dir = os.path.join(work, "data")
    answers = []
    db = Database(binary, data_dir)
    try:
        for sql in statements:
            if sql == "#restart":
                db.restart()
                continue
            answers.append((sql, db.execute(sql)))
            # The approximate mode is answered as well, so that the two
            # modes are compared too.
            answers.append((sql + " [ann]", db.execute(sql, mode="ann")))
    finally:
        db.close()
        shutil.rmtree(work, ignore_errors=True)
    return answers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", default="/tmp/mvdb_baseline")
    parser.add_argument(
        "--new",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "minivectordb",
            "target",
            "release",
            "minivectordb",
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    old = run(args.old, STATEMENTS)
    new = run(args.new, STATEMENTS)

    if len(old) != len(new):
        print("the two runs did not answer the same number of statements")
        return 1

    failures = 0
    for (sql_a, answer_a), (sql_b, answer_b) in zip(old, new):
        assert sql_a == sql_b, (sql_a, sql_b)
        if answer_a != answer_b:
            failures += 1
            print("DIFFERENT  %s" % sql_a)
            print("  old: %s" % answer_a[:400])
            print("  new: %s" % answer_b[:400])
        elif args.verbose:
            print("ok        %s" % sql_a)

    print()
    print("%d of %d answers are identical" % (len(old) - failures, len(old)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
