#!/usr/bin/env python3
"""Check the vector rules that the public test pack does not cover.

The public pack explicitly says that the formal requirements still apply
even when the public pack has no case for them:

- reject NaN, Infinity, a wrong dimension and a float32 overflow;
- a failed statement must not take effect in part;
- the modifications that succeeded before an explicit transaction can still
  be committed or rolled back.

This script drives the database over the standard input and output
protocol and checks each rule on its own, and then checks that the
database is still usable after every rejection.

Usage:
    tools/check_vector_rules.py [--binary PATH]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

DIM = 8


class Db:
    def __init__(self, binary, data_dir):
        self.proc = subprocess.Popen(
            [binary, "--data-dir", data_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = self.proc.stdout.readline()
        if not ready:
            raise RuntimeError("the database closed its output: %s" % self.proc.stderr.read()[-2000:])

    def execute(self, sql, mode="exact"):
        request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the database exited: %s" % self.proc.stderr.read()[-2000:])
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        except Exception:
            self.proc.kill()


def check(name, ok, detail=""):
    print("%-52s %s%s" % (name, "ok" if ok else "FAIL", (" - " + detail) if detail and not ok else ""))
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "minivectordb",
            "target",
            "release",
            "minivectordb",
        ),
    )
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-rules-")
    data_dir = os.path.join(work, "data")
    passed = True
    try:
        db = Db(args.binary, data_dir)
        try:
            good = ",".join(str(i) for i in range(DIM))
            db.execute("CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % DIM)

            # The values that a vector must not hold.
            bad_values = [
                ("NaN", "[NaN," + ",".join(["0"] * (DIM - 1)) + "]"),
                ("Infinity", "[Infinity," + ",".join(["0"] * (DIM - 1)) + "]"),
                ("-Infinity", "[-Infinity," + ",".join(["0"] * (DIM - 1)) + "]"),
                ("nan in a later position", "[" + ",".join(["0"] * (DIM - 1)) + ",NaN]"),
                ("inf in a later position", "[" + ",".join(["0"] * (DIM - 1)) + ",Infinity]"),
            ]
            for label, value in bad_values:
                response = db.execute("INSERT INTO t VALUES (1, '%s');" % value)
                passed &= check("a vector with %s is rejected" % label, response.get("ok") is False, str(response))
                passed &= check(
                    "the rejected insert with %s left no row" % label,
                    db.execute("SELECT id FROM t;").get("rows") == [],
                )

            # A vector of the wrong dimension.
            for label, value in [
                ("too few dimensions", "[1,2,3]"),
                ("too many dimensions", "[" + ",".join(["1"] * (DIM + 1)) + "]"),
                ("a dimension that is not a number", "[1,2,x]"),
            ]:
                response = db.execute("INSERT INTO t VALUES (2, '%s');" % value)
                passed &= check("a vector with %s is rejected" % label, response.get("ok") is False, str(response))
                passed &= check(
                    "the rejected insert with %s left no row" % label,
                    db.execute("SELECT id FROM t;").get("rows") == [],
                )

            # A float32 overflow: 1e39 does not fit in a float32.
            overflow = "[1e39," + ",".join(["0"] * (DIM - 1)) + "]"
            response = db.execute("INSERT INTO t VALUES (3, '%s');" % overflow)
            passed &= check("a vector with a float32 overflow is rejected", response.get("ok") is False, str(response))
            passed &= check(
                "the rejected overflow insert left no row",
                db.execute("SELECT id FROM t;").get("rows") == [],
            )

            # The distance of a query vector is checked in the same way.
            response = db.execute("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[NaN,1,2,3,4,5,6,7]') LIMIT 10;")
            passed &= check("a query vector with NaN is rejected", response.get("ok") is False, str(response))

            # The good path still works after all of those rejections.
            response = db.execute("INSERT INTO t VALUES (10, '[%s]');" % good)
            passed &= check("a valid vector is still accepted", response.get("ok") is True, str(response))
            response = db.execute("SELECT id FROM t;")
            passed &= check("the table holds only the valid row", response.get("rows") == [[10]], str(response))

            # A statement that fails must not take effect in part: a multi row
            # insert where one row is bad must leave nothing behind.
            response = db.execute(
                "INSERT INTO t VALUES (20, '[%s]'), (21, '[NaN,1,2,3,4,5,6,7]');" % good
            )
            passed &= check("a multi row insert with one bad row fails", response.get("ok") is False, str(response))
            response = db.execute("SELECT id FROM t ORDER BY id;")
            passed &= check(
                "the failed multi row insert left nothing behind",
                response.get("rows") == [[10]],
                str(response),
            )

            # An update that fails must not take effect in part either.
            response = db.execute("UPDATE t SET v = '[NaN,1,2,3,4,5,6,7]' WHERE id = 10;")
            passed &= check("an update to a bad vector fails", response.get("ok") is False, str(response))
            response = db.execute("SELECT id, v FROM t WHERE id = 10;")
            passed &= check(
                "the failed update left the old vector in place",
                response.get("rows") == [[10, [float(i) for i in range(DIM)]]],
                str(response),
            )

            # The rows before an explicit transaction can still be committed.
            response = db.execute("BEGIN;")
            passed &= check("a transaction can be started", response.get("ok") is True, str(response))
            response = db.execute("INSERT INTO t VALUES (30, '[%s]');" % good)
            passed &= check("an insert inside a transaction succeeds", response.get("ok") is True, str(response))
            response = db.execute("ROLLBACK;")
            passed &= check("a transaction can be rolled back", response.get("ok") is True, str(response))
            response = db.execute("SELECT id FROM t ORDER BY id;")
            passed &= check(
                "the rolled back row is gone",
                response.get("rows") == [[10]],
                str(response),
            )
        finally:
            db.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n%s" % ("all rules hold" if passed else "some rules do not hold"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
