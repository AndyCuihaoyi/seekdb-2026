#!/usr/bin/env python3
"""Run the public MiniVectorDB cases against the database under test.

The cases are the mysqltest style sample set that the organiser published in
the `2026-competition-round2` branch of `oceanbase/seekdb`. Every case starts
from an empty data directory, the statements of the `.test` file are executed
one by one, and the answers are compared with the `.result` file.

The comparison follows the semantics that the published README describes:

* `{}` means the statement has to succeed; the fields are not constrained.
* `affected_rows` has to match exactly.
* `columns` has to match exactly, including the aliases and the order.
* `rows` is compared element by element, unless `unordered` is set, in which
  case the rows are compared as a multiset.
* A distance is compared with a tolerance, everything else is compared
  exactly.

The database is started with the `jsonl` output format, which writes one JSON
object per statement. The fields of that object are the fields of the
published `.result` entries, so the comparison is direct.

Usage:
    tools/run_public_cases.py [--cases DIR] [--binary PATH] [--verbose]

The script exits with a non-zero status when a check fails.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# The absolute and relative tolerance of a float comparison.
FLOAT_TOLERANCE = 1e-5


class Failure(Exception):
    """A check that did not hold."""


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_value(expected, actual, where):
    """Compare one value with its expectation."""
    if expected is None:
        if actual is not None:
            raise Failure("%s: expected NULL, got %r" % (where, actual))
        return
    if actual is None:
        raise Failure("%s: expected %r, got NULL" % (where, expected))
    if is_number(expected) and is_number(actual):
        diff = abs(float(expected) - float(actual))
        if diff <= FLOAT_TOLERANCE:
            return
        scale = max(abs(float(expected)), abs(float(actual)), 1.0)
        if diff / scale <= FLOAT_TOLERANCE:
            return
        raise Failure("%s: expected %r, got %r" % (where, expected, actual))
    if expected != actual:
        raise Failure("%s: expected %r, got %r" % (where, expected, actual))


def compare_rows(expected_rows, actual_rows, unordered, where):
    if unordered:
        remaining = list(actual_rows)
        for want in expected_rows:
            for i, got in enumerate(remaining):
                try:
                    for column, (e, a) in enumerate(zip(want, got)):
                        compare_value(e, a, "%s column %d" % (where, column))
                except Failure:
                    continue
                remaining.pop(i)
                break
            else:
                raise Failure("%s: the row %r is missing (got %r)" % (where, want, remaining))
        if remaining:
            raise Failure("%s: unexpected extra rows %r" % (where, remaining))
        return
    if len(expected_rows) != len(actual_rows):
        raise Failure(
            "%s: expected %d rows, got %d" % (where, len(expected_rows), len(actual_rows))
        )
    for i, (want, got) in enumerate(zip(expected_rows, actual_rows)):
        if len(want) != len(got):
            raise Failure(
                "%s row %d: expected %d columns, got %d"
                % (where, i, len(want), len(got))
            )
        for column, (e, a) in enumerate(zip(want, got)):
            compare_value(e, a, "%s row %d column %d" % (where, i, column))


def compare_answer(expected, actual, where):
    """Compare the answer of one statement with its expectation."""
    if not isinstance(actual, dict):
        raise Failure("%s: the answer is not an object: %r" % (where, actual))
    if "error" in actual:
        raise Failure("%s: the statement failed: %r" % (where, actual["error"]))

    if "affected_rows" in expected:
        got = actual.get("affected_rows")
        if got != expected["affected_rows"]:
            raise Failure(
                "%s: affected_rows expected %r, got %r"
                % (where, expected["affected_rows"], got)
            )
    if "columns" in expected:
        got = actual.get("columns")
        if got != expected["columns"]:
            raise Failure("%s: columns expected %r, got %r" % (where, expected["columns"], got))
    if "rows" in expected:
        got = actual.get("rows")
        if got is None:
            got = []
        compare_rows(
            expected["rows"], got, bool(expected.get("unordered")), where
        )


class Database:
    """A running instance of the database under test."""

    def __init__(self, binary, data_dir, mode="exact", nlist=32, nprobe=None):
        env = dict(os.environ)
        env["MINIVECDB_DATA_DIR"] = data_dir
        env["MINIVECDB_MODE"] = mode
        env["MINIVECDB_OUTPUT_FORMAT"] = "jsonl"
        env["MINIVECDB_READY_SINK"] = "stderr"
        env["MINIVECDB_ANN_NLIST"] = str(nlist)
        if nprobe is None:
            env.pop("MINIVECDB_ANN_NPROBE", None)
        else:
            env["MINIVECDB_ANN_NPROBE"] = str(nprobe)
        self.proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        line = self.proc.stderr.readline()
        if "ready" not in line:
            raise Failure("the database did not report readiness: %r" % line)

    def run(self, sql):
        """Run one statement and return the answer object."""
        self.proc.stdin.write(sql.rstrip() + "\n")
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        if not raw:
            raise Failure("the database closed its output while running %r" % sql)
        raw = raw.strip()
        if not raw:
            raise Failure("the statement %r produced no answer" % sql)
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise Failure("the answer of %r is not JSON: %r (%s)" % (sql, raw, exc))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def read_statements(path):
    """Read the statements of a `.test` file."""
    statements = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                statements.append(line)
    return statements


def run_case(case_id, cases_dir, binary, mode, verbose):
    """Run one case and return the number of failures."""
    test_path = os.path.join(cases_dir, "%s.test" % case_id)
    result_path = os.path.join(cases_dir, "%s.result" % case_id)
    with open(result_path, "r", encoding="utf-8") as fh:
        expectations = json.load(fh)
    statements = read_statements(test_path)
    if len(statements) != len(expectations):
        raise Failure(
            "the case %s has %d statements but %d expectations"
            % (case_id, len(statements), len(expectations))
        )

    work = tempfile.mkdtemp(prefix="mvdb-case-")
    failures = []
    db = None
    try:
        db = Database(binary, os.path.join(work, "data"), mode=mode)
        for i, (sql, expected) in enumerate(zip(statements, expectations)):
            try:
                actual = db.run(sql)
                compare_answer(expected, actual, "statement %d (%s)" % (i + 1, sql))
                if verbose:
                    print("ok    %-22s %d" % (case_id, i + 1))
            except Failure as exc:
                failures.append(str(exc))
                print("FAIL  %-22s %d: %s" % (case_id, i + 1, exc), file=sys.stderr)
    finally:
        if db is not None:
            db.close()
        shutil.rmtree(work, ignore_errors=True)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "public_cases"),
        help="the directory that holds the case files",
    )
    parser.add_argument(
        "--binary",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "minivectordb",
            "target",
            "release",
            "minivectordb",
        ),
        help="the database binary",
    )
    parser.add_argument(
        "--case", action="append", default=None, help="run only this case"
    )
    parser.add_argument("--mode", default="exact", choices=["exact", "ann"])
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(args.cases):
        print("error: the case directory %s does not exist" % args.cases, file=sys.stderr)
        return 2
    if not os.access(args.binary, os.X_OK):
        print("error: the binary %s is not executable" % args.binary, file=sys.stderr)
        return 2

    manifest_path = os.path.join(args.cases, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        case_ids = [c["id"] for c in manifest.get("cases", [])]
    else:
        case_ids = sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(args.cases)
            if f.endswith(".test")
        )
    if args.case:
        case_ids = [c for c in case_ids if c in args.case]
        if not case_ids:
            print("error: no case matches %r" % args.case, file=sys.stderr)
            return 2

    total_failures = 0
    for case_id in case_ids:
        try:
            failures = run_case(case_id, args.cases, args.binary, args.mode, args.verbose)
        except Failure as exc:
            print("FAIL  %-22s %s" % (case_id, exc), file=sys.stderr)
            total_failures += 1
            continue
        if failures:
            total_failures += len(failures)
        else:
            print("ok    %-22s" % case_id)

    if total_failures:
        print("\n%d check(s) failed" % total_failures, file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
