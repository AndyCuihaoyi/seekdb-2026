"""Compare the fast primary key path with the general path.

The fast path answers a statement that looks up one row by its primary key
through the primary key index. The general path reads the whole table and
throws all of it away but one row.

Both have to produce the same answer, for every state of the data: after an
insert, after an update, after a delete, inside a transaction and after a
restart.

The general path is forced by asking for the same filter with a statement
the fast path refuses. The fast path only takes a statement whose ordering
is the primary key itself, so a statement that orders by another column
falls back to the scan.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BIN = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.abspath(
    "minivectordb/target/release/minivectordb")


class Db:
    def __init__(self, binary, workdir, mode="exact"):
        self.binary = binary
        self.workdir = workdir
        self.mode = mode
        env = dict(os.environ)
        env.update(MINIVECDB_DATA_DIR=workdir, MINIVECDB_MODE=mode)
        self.proc = subprocess.Popen(
            [binary, "--data-dir", workdir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        self.proc.stdout.readline()
        self.id = 0

    def run(self, sql):
        self.id += 1
        self.proc.stdin.write(json.dumps({
            "id": self.id, "op": "execute", "sql": sql,
            "search_mode": self.mode}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def close(self):
        try:
            self.proc.stdin.write('{"id":99999,"op":"shutdown"}\n')
            self.proc.stdin.flush()
            self.proc.stdout.readline()
        except Exception:
            pass
        self.proc.kill()
        self.proc.wait()


def normalise(answer):
    """The answer of a statement, without the request identifier."""
    if not answer.get("ok"):
        return {"error": answer.get("error")}
    return {"columns": answer.get("columns"), "rows": answer.get("rows")}


def compare(label, fast, general):
    if normalise(fast) != normalise(general):
        print("MISMATCH  %s" % label)
        print("   fast   : %s" % json.dumps(normalise(fast), sort_keys=True)[:400])
        print("   general: %s" % json.dumps(normalise(general), sort_keys=True)[:400])
        return False
    print("ok        %s" % label)
    return True


def main():
    workdir = tempfile.mkdtemp(prefix="pkeq")
    ok = True

    db = Db(BIN, workdir)
    db.run("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));")
    for i in range(1, 51):
        db.run("INSERT INTO t VALUES (%d, 'c%d', '[%d.0,0.0,0.0,0.0]');" % (i, i % 4, i))

    # The fast statement looks up one row by its primary key. The general
    # statement asks for the same row but orders by another column, which
    # the fast path refuses, so the whole table is scanned.
    cases = [
        ("a row that exists", "SELECT id, cat FROM t WHERE id = 17",
         "SELECT id, cat FROM t WHERE id = 17 ORDER BY cat"),
        ("a row that does not exist", "SELECT id, cat FROM t WHERE id = 9999",
         "SELECT id, cat FROM t WHERE id = 9999 ORDER BY cat"),
        ("the first row", "SELECT id FROM t WHERE id = 1",
         "SELECT id FROM t WHERE id = 1 ORDER BY cat"),
        ("the last row", "SELECT id FROM t WHERE id = 50",
         "SELECT id FROM t WHERE id = 50 ORDER BY cat"),
        ("a negative key", "SELECT id FROM t WHERE id = -5",
         "SELECT id FROM t WHERE id = -5 ORDER BY cat"),
        ("a key of zero", "SELECT id FROM t WHERE id = 0",
         "SELECT id FROM t WHERE id = 0 ORDER BY cat"),
    ]
    for label, fast_sql, general_sql in cases:
        ok = compare(label, db.run(fast_sql + ";"), db.run(general_sql + ";")) and ok

    # An update changes the row the fast path finds.
    db.run("UPDATE t SET cat = 'updated' WHERE id = 17;")
    ok = compare("after an update", db.run("SELECT id, cat FROM t WHERE id = 17;"),
                 db.run("SELECT id, cat FROM t WHERE id = 17 ORDER BY cat;")) and ok

    # A delete takes the row away.
    db.run("DELETE FROM t WHERE id = 17;")
    ok = compare("after a delete", db.run("SELECT id, cat FROM t WHERE id = 17;"),
                 db.run("SELECT id, cat FROM t WHERE id = 17 ORDER BY cat;")) and ok

    # A row that the transaction holds is preferred over the committed one.
    db.run("BEGIN;")
    db.run("INSERT INTO t VALUES (999, 'staged', '[9.0,0.0,0.0,0.0]');")
    ok = compare("a staged row", db.run("SELECT id, cat FROM t WHERE id = 999;"),
                 db.run("SELECT id, cat FROM t WHERE id = 999 ORDER BY cat;")) and ok
    db.run("COMMIT;")

    # A row that the transaction deleted is not visible.
    db.run("BEGIN;")
    db.run("DELETE FROM t WHERE id = 5;")
    ok = compare("a deleted row", db.run("SELECT id FROM t WHERE id = 5;"),
                 db.run("SELECT id FROM t WHERE id = 5 ORDER BY cat;")) and ok
    db.run("ROLLBACK;")

    db.close()

    # The answers survive a restart.
    db = Db(BIN, workdir)
    ok = compare("after a restart", db.run("SELECT id, cat FROM t WHERE id = 17;"),
                 db.run("SELECT id, cat FROM t WHERE id = 17 ORDER BY cat;")) and ok
    ok = compare("a staged row after a restart",
                 db.run("SELECT id, cat FROM t WHERE id = 999;"),
                 db.run("SELECT id, cat FROM t WHERE id = 999 ORDER BY cat;")) and ok
    db.close()

    shutil.rmtree(workdir, ignore_errors=True)
    print()
    print("all checks passed" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
