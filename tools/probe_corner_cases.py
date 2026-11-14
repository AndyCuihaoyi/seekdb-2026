#!/usr/bin/env python3
"""Run the corner cases of the statement against the judge transport.

The classification run showed that every phase of a large load is well
within the budget of the judge, so the timeout of the platform is not a
cost that grows with the size of the data set. What is left is a
**statement** that does not answer at all: a loop that never ends, a
statement that is answered with something the judge cannot parse, or a
statement that is not answered because the process is blocked.

This script runs the corner cases that the statement names, one at a
time, through the **official** transport, and reports the wall clock of
each one. A case that takes more than a second is named, because a case
that hangs is the case the platform saw.

The cases are the ones the statement calls out:

* the illegal vectors: NaN, Infinity, a dimension that does not match,
  a value that overflows float32;
* the atomicity of a failed statement, inside a transaction;
* the rollback of a transaction;
* the maintenance of the index across an insert, an update and a delete;
* the columns that the inverted index cannot serve: a column with more
  distinct values than `MAX_DISTINCT`, so that the index is given up on,
  and a column with few, so that the index is used;
* a long text column, the blind spot of the gate that is run today.

Usage:
    tools/probe_corner_cases.py --binary PATH [--timeout S]
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "/tmp/official/protocol_check")

from _transport import Session  # noqa: E402


CONFIG_PATH = "/tmp/official/protocol_check/config.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


class Case:
    """One corner case: a list of statements and what to expect."""

    def __init__(self, name, statements, expect_fail=(), note=""):
        self.name = name
        self.statements = statements
        self.expect_fail = set(expect_fail)
        self.note = note


def build_cases(dim, text):
    """The corner cases of the statement."""
    vec = ",".join(str(i) for i in range(dim))
    bad_vec = ",".join(str(i) for i in range(dim - 1))
    long_text = "x" * text

    cases = [
        Case(
            "vector_nan",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % dim,
             "INSERT INTO t VALUES (1, '[%s,NaN]');" % vec],
            expect_fail=[1],
            note="NaN must be refused",
        ),
        Case(
            "vector_infinity",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % dim,
             "INSERT INTO t VALUES (1, '[%s,Infinity]');" % vec],
            expect_fail=[1],
            note="Infinity must be refused",
        ),
        Case(
            "vector_dimension_mismatch",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % dim,
             "INSERT INTO t VALUES (1, '[%s]');" % bad_vec],
            expect_fail=[1],
            note="a dimension that does not match must be refused",
        ),
        Case(
            "vector_float32_overflow",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % dim,
             "INSERT INTO t VALUES (1, '[%s,1e300]');" % vec],
            expect_fail=[1],
            note="a value that overflows float32 must be refused",
        ),
        Case(
            "atomic_failed_statement",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % dim,
             "BEGIN;",
             "INSERT INTO t VALUES (1, '[%s]');" % vec,
             "INSERT INTO t VALUES (1, '[%s]');" % vec,
             "COMMIT;",
             "SELECT id FROM t;"],
            expect_fail=[3],
            note="the failed statement must leave nothing behind",
        ),
        Case(
            "rollback_keeps_the_table",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % dim,
             "INSERT INTO t VALUES (1, '[%s]');" % vec,
             "BEGIN;",
             "INSERT INTO t VALUES (2, '[%s]');" % vec,
             "ROLLBACK;",
             "SELECT id FROM t;"],
            note="the rolled back row must be gone",
        ),
        Case(
            "index_maintenance",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(%d));" % dim,
             "INSERT INTO t VALUES (1, '[%s]');" % vec,
             "CREATE VECTOR INDEX idx_t ON t(v);",
             "INSERT INTO t VALUES (2, '[%s]');" % vec,
             "UPDATE t SET v = '[%s]' WHERE id = 2;" % vec,
             "DELETE FROM t WHERE id = 1;",
             "SELECT id FROM t;"],
            note="the index must follow the data",
        ),
        Case(
            "wide_column_gives_the_index_up",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(32), "
             "v VECTOR(%d));" % dim,
             "BEGIN;"]
            + ["INSERT INTO t VALUES (%d, 'c%d', '[%s]');" % (i, i, vec)
               for i in range(1200)]
            + ["COMMIT;",
               "SELECT id FROM t WHERE cat = 'c7' "
               "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % vec],
            note="a column with more than MAX_DISTINCT values is not indexed",
        ),
        Case(
            "narrow_column_uses_the_index",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(32), "
             "v VECTOR(%d));" % dim,
             "BEGIN;"]
            + ["INSERT INTO t VALUES (%d, 'c%d', '[%s]');" % (i, i % 8, vec)
               for i in range(1200)]
            + ["COMMIT;",
               "SELECT id FROM t WHERE cat = 'c7' "
               "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % vec],
            note="a column with few values is indexed",
        ),
        Case(
            "long_text_column",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, body VARCHAR(%d), "
             "v VECTOR(%d));" % (text, dim),
             "BEGIN;"]
            + ["INSERT INTO t VALUES (%d, '%s', '[%s]');" % (i, long_text, vec)
               for i in range(500)]
            + ["COMMIT;",
               "SELECT id FROM t WHERE body = '%s' LIMIT 5;" % long_text],
            note="a long text column must not make a statement hang",
        ),
        Case(
            "many_updates_in_one_transaction",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(32), "
             "v VECTOR(%d));" % dim,
             "BEGIN;"]
            + ["INSERT INTO t VALUES (%d, 'c%d', '[%s]');" % (i, i % 8, vec)
               for i in range(500)]
            + ["UPDATE t SET cat = 'c9' WHERE id = %d;" % i
               for i in range(0, 500, 10)]
            + ["ROLLBACK;",
               "SELECT id FROM t WHERE cat = 'c9' LIMIT 5;"],
            note="the updates of a transaction must not make it slow",
        ),
        Case(
            "delete_many_rows",
            ["CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(32), "
             "v VECTOR(%d));" % dim,
             "BEGIN;"]
            + ["INSERT INTO t VALUES (%d, 'c%d', '[%s]');" % (i, i % 8, vec)
               for i in range(2000)]
            + ["COMMIT;",
               "DELETE FROM t WHERE cat = 'c3';",
               "SELECT id FROM t WHERE cat = 'c3' LIMIT 5;"],
            note="a delete of many rows must not make a statement hang",
        ),
    ]
    return cases


def run_case(session, case, timeout):
    """Run one case, and report the time of every statement."""
    results = []
    for index, sql in enumerate(case.statements):
        started = time.monotonic()
        try:
            response = session.request(sql, "exact")
        except TimeoutError as exc:
            results.append({
                "index": index,
                "sql": sql[:80],
                "seconds": round(time.monotonic() - started, 3),
                "outcome": "TIMEOUT: %s" % exc,
            })
            return results, False
        except Exception as exc:
            results.append({
                "index": index,
                "sql": sql[:80],
                "seconds": round(time.monotonic() - started, 3),
                "outcome": "ERROR: %s" % exc,
            })
            return results, False
        took = time.monotonic() - started
        ok = response.get("ok") is True
        results.append({
            "index": index,
            "sql": sql[:80],
            "seconds": round(took, 3),
            "outcome": "ok" if ok else "refused",
        })
        if index in case.expect_fail and ok:
            results[-1]["outcome"] = "UNEXPECTED SUCCESS"
            return results, False
    return results, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--only", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    config = dict(config)
    config["request_timeout"] = args.timeout

    work = tempfile.mkdtemp(prefix="mvdb-corner-")
    log_path = os.path.join(work, "stderr.log")
    report = {"args": vars(args), "cases": {}}
    slow = []

    try:
        for case in build_cases(args.dim, args.text):
            if args.only and args.only not in case.name:
                continue
            session = Session(
                config,
                log_path,
                command=["sh", "-c",
                         "exec %s --data-dir {data_dir}" % args.binary],
                case_timeout=3600,
            )
            try:
                session.start()
                results, passed = run_case(session, case, args.timeout)
                worst = max((r["seconds"] for r in results), default=0.0)
                report["cases"][case.name] = {
                    "note": case.note,
                    "passed": passed,
                    "worst_statement_s": round(worst, 3),
                    "statements": results,
                }
                if not passed or worst > 1.0:
                    slow.append({
                        "case": case.name,
                        "passed": passed,
                        "worst_statement_s": round(worst, 3),
                    })
                print("%-34s %-6s worst %.3fs  %s" % (
                    case.name,
                    "PASS" if passed else "FAIL",
                    worst,
                    case.note,
                ))
                for entry in results:
                    if entry["outcome"] not in ("ok", "refused"):
                        print("    statement %d: %s" % (entry["index"], entry["outcome"]))
            except Exception as exc:
                report["cases"][case.name] = {"error": str(exc)}
                slow.append({"case": case.name, "error": str(exc)})
                print("%-34s %-6s %s" % (case.name, "ERROR", exc))
            finally:
                try:
                    session.close()
                except Exception:
                    pass
    finally:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                report["stderr_tail"] = handle.read()[-4000:]
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)

    report["slow_or_failed"] = slow
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print()
    print("slow or failed: %s" % json.dumps(slow, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
