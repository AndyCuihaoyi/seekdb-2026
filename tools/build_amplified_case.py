#!/usr/bin/env python3
"""Build an amplified copy of a public case, and run it with the official judge.

The public case `vector_index_maintenance` is three rows and a dimension
of two. It cannot time out, so it cannot reproduce the failure of the
platform: the failure needs a data set that is big enough.

This script builds an **amplified** copy of a case - the same statements,
in the same order, with the same directives (`--restart`, `--mode ann`) -
with a data set that is large, and it runs it with the **official** runner,
`/tmp/official/protocol_check/run.py`, so that the judge itself is the one
that decides whether the case passes.

**The shape is the official one, statement for statement.** The public
case is read from the official package and its statement sequence is
reproduced literally:

    CREATE TABLE -> batch INSERT -> CREATE VECTOR INDEX -> UPDATE ->
    DELETE -> INSERT -> SELECT -> --restart -> SELECT ->
    --mode ann -> SELECT

Only the *size* of the data set changes. The query of the official case
has **no WHERE clause** - it is `ORDER BY distance LIMIT 10` - and the
copy keeps it that way. An earlier version of this script added a filter
of its own, `WHERE category = 'c3'`, and that was a mistake: it measured a
shape the judge never runs. The `--filter` switch keeps the old shape
available for comparison, but the default is the faithful one.

The expected answers are **computed by the exact path**, not written by
hand and not taken from the approximate path: the script asks the
database for the exact answer of every statement, and the file of the
expectations is written from those answers. The approximate answers then
have to match them, which is the point of the case.

The package that the script writes is a directory with a `manifest.json`
and a `cases/` directory, which is the shape the official runner
expects; `--package` points the runner at it.

Usage:
    tools/build_amplified_case.py --binary PATH --rows N --text T --dim D
                                   --out DIR [--repeat N] [--case ID]
                                   [--filter] [--batch N]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# The directory of the public package of the organiser.
OFFICIAL_CASES = "/tmp/official/mysqltest/cases"


def official_statements(case):
    """The statements of a public case, read from the official package.

    The directives are kept, because they are part of the shape: the
    `--restart` and the `--mode ann` of the public case are the ones the
    judge runs.
    """
    path = os.path.join(OFFICIAL_CASES, case + ".test")
    with open(path, "r", encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    return lines


def ask_all(binary, statements, data_dir, force_mode=None):
    """Run the statements of a case against a fresh database.

    The directives `--restart` and `--mode ann` are honoured, exactly as
    the official runner honours them. `force_mode` overrides the mode
    that the directives set, which is how the expectations are computed
    with the exact path.
    """
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )
    ready = proc.stdout.readline()
    if '"ready"' not in ready:
        raise RuntimeError("the database did not say ready: %r" % ready)

    mode = "exact"
    answers = []
    for sql in statements:
        if sql == "--restart":
            proc.stdin.write(json.dumps({"id": 0, "op": "shutdown"}) + "\n")
            proc.stdin.flush()
            proc.stdout.readline()
            proc.stdin.close()
            proc.wait(timeout=300)
            proc = subprocess.Popen(
                [binary, "--data-dir", data_dir],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            proc.stdout.readline()
            mode = "exact"
            continue
        if sql in ("--mode exact", "--mode ann"):
            mode = sql.split()[1]
            continue
        use = force_mode or mode
        proc.stdin.write(
            json.dumps({"id": 1, "op": "execute", "sql": sql,
                        "search_mode": use}) + "\n")
        proc.stdin.flush()
        answers.append(json.loads(proc.stdout.readline()))
    proc.stdin.close()
    proc.wait(timeout=300)
    return answers


def build_statements(args):
    """The statement sequence of the amplified case.

    The sequence is the sequence of the public case: the table, the rows,
    the index, the update, the delete, the insert, the query, the
    restart, the query again, and the query in the approximate mode.
    """
    body = "x" * args.text
    probe = ",".join(str(i % 100) for i in range(args.dim))
    column = "category VARCHAR(8), content VARCHAR(%d)" % max(args.text, 1)

    statements = [
        "CREATE TABLE docs (id BIGINT PRIMARY KEY, %s, embedding VECTOR(%d));"
        % (column, args.dim)
    ]
    for first in range(0, args.rows, args.batch):
        values = []
        for row_id in range(first, min(first + args.batch, args.rows)):
            vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(args.dim))
            values.append(
                "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
            )
        statements.append("INSERT INTO docs VALUES " + ",".join(values) + ";")

    # The three write statements of the public case, in its order.
    statements.append("CREATE VECTOR INDEX docs_vec ON docs(embedding);")
    statements.append(
        "UPDATE docs SET embedding = '[%s]' WHERE id = 1;" % probe
    )
    statements.append("DELETE FROM docs WHERE id = 2;")
    statements.append(
        "INSERT INTO docs VALUES (%d,'c0','x','[%s]');" % (args.rows, probe)
    )

    # The query of the public case, verbatim. It has **no filter**.
    where = " WHERE category = 'c3'" if args.filter else ""
    query = (
        "SELECT id, L2_DISTANCE(embedding, '[%s]') AS distance FROM docs"
        "%s ORDER BY distance LIMIT 10;" % (probe, where)
    )
    statements.append(query)
    statements.append("--restart")
    statements.append(query)
    statements.append("--mode ann")
    statements.extend([query] * args.repeat)
    return statements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--repeat", type=int, default=1,
                        help="the number of the approximate queries at the end")
    parser.add_argument("--out", default=None)
    parser.add_argument("--case", default="vector_index_maintenance_amplified")
    parser.add_argument("--official-case", default="vector_index_maintenance",
                        help="the public case whose shape is copied")
    parser.add_argument("--filter", action="store_true",
                        help="add a WHERE clause: the shape of the earlier "
                             "version of this script, not the official one")
    parser.add_argument("--print-shape", action="store_true",
                        help="print the statement sequence and exit")
    args = parser.parse_args()

    # The shape of the public case, for the record.
    official = official_statements(args.official_case)

    if args.print_shape:
        print("the official case %s:" % args.official_case)
        for line in official:
            print("  %s" % line)
        print("the amplified case:")
        for line in build_statements(args):
            print("  %s" % (line[:160] + ("..." if len(line) > 160 else "")))
        return 0

    if not args.out:
        parser.error("--out is required")

    work = tempfile.mkdtemp(prefix="mvdb-amplified-")
    try:
        data_dir = os.path.join(work, "data")
        statements = build_statements(args)

        # The expectations come from the **exact** path. The approximate
        # answers of the case have to match them, and that is what the
        # official runner checks.
        #
        # The directives do not answer anything, so the answers are paired
        # with the statements that are not directives: pairing them with
        # the whole list would shift every answer by one.
        exact = ask_all(args.binary, statements, data_dir, force_mode="exact")
        expectations = []
        answered = iter(exact)
        for sql in statements:
            if sql.startswith("--"):
                continue
            answer = next(answered)
            if answer.get("ok") is not True:
                raise RuntimeError("the statement failed: %s -> %s" % (sql, answer))
            expectations.append(answer)
        expected_count = sum(1 for s in statements if not s.startswith("--"))
        if len(expectations) != expected_count:
            raise RuntimeError("the answers do not match the statements: %d against %d"
                               % (len(expectations), expected_count))
        print("the exact answers of %d statements were collected" % len(expectations))

        # The package the official runner expects.
        out = os.path.abspath(args.out)
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(os.path.join(out, "cases"))
        with open(os.path.join(out, "cases", args.case + ".test"), "w",
                  encoding="utf-8") as handle:
            handle.write("\n".join(statements) + "\n")
        with open(os.path.join(out, "cases", args.case + ".result"), "w",
                  encoding="utf-8") as handle:
            handle.write(json.dumps(expectations, ensure_ascii=False, indent=2) + "\n")
        with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "version": "minivectordb-public-v4",
                "scope": "amplified",
                "cases": [{
                    "id": args.case,
                    "group": "vector",
                    "test": "cases/%s.test" % args.case,
                    "result": "cases/%s.result" % args.case,
                    "statements": len(statements),
                }],
            }, ensure_ascii=False, indent=2) + "\n")
        size = os.path.getsize(os.path.join(out, "cases", args.case + ".test"))
        print("the package was written to %s (the test file is %.1f MiB)"
              % (out, size / (1024.0 * 1024.0)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
