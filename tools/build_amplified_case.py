#!/usr/bin/env python3
"""Build an amplified copy of a public case, and run it with the official judge.

The public case `vector_index_maintenance` is three rows and a dimension
of two. It cannot time out, so it cannot reproduce the failure of the
platform: the failure needs a data set that is big enough, and a text
column that is long enough for the block cache to thrash.

This script builds an **amplified** copy of a case - the same shape, the
same directives (`--restart`, `--mode ann`), the same filter - with a
data set that is large, and it runs it with the **official** runner,
`/tmp/official/protocol_check/run.py`, so that the judge itself is the
one that decides whether the case passes.

The expected answers are **computed by the exact path**, not written by
hand and not taken from the approximate path: the script asks the
database for the exact answer of every statement, and the file of the
expectations is written from those answers. The approximate answers then
have to match them, which is the point of the case.

Two shapes are built:

* the **correctness** shape - a few queries. It passes when the answers
  of the approximate mode and of the exact mode are the same.
* the **timeout** shape - the same data, with the query repeated. The
  judge charges every statement sixty seconds and every case six
  hundred, so a case that repeats the query enough times goes over the
  budget of the case. The repetition is the cheapest way to amplify: the
  queries are one line each, while the data is the whole file.

The package that the script writes is a directory with a `manifest.json`
and a `cases/` directory, which is the shape the official runner
expects; `--package` points the runner at it.

Usage:
    tools/build_amplified_case.py --binary PATH --rows N --text T --dim D
                                   --out DIR [--repeat N] [--case ID]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--repeat", type=int, default=1,
                        help="the number of the approximate queries at the end")
    parser.add_argument("--out", required=True)
    parser.add_argument("--case", default="vector_index_maintenance_amplified")
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-amplified-")
    try:
        data_dir = os.path.join(work, "data")
        body = "x" * args.text
        probe = ",".join(str(i % 100) for i in range(args.dim))

        # The shape of `vector_index_maintenance`, at the size that
        # matters: a table with a filter column, a long text column and a
        # vector column; a write; the creation of the index; an update; a
        # delete; an insert; a query; a restart; a query; and the switch
        # to the approximate mode.
        statements = [
            "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
            "content VARCHAR(%d), embedding VECTOR(%d));" % (args.text, args.dim)
        ]
        for first in range(0, args.rows, args.batch):
            values = []
            for row_id in range(first, min(first + args.batch, args.rows)):
                vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(args.dim))
                values.append(
                    "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
                )
            statements.append("INSERT INTO docs VALUES " + ",".join(values) + ";")
        statements.append("CREATE VECTOR INDEX docs_vec ON docs(embedding);")
        statements.append(
            "UPDATE docs SET embedding = '[%s]' WHERE id = 1;" % probe
        )
        statements.append("DELETE FROM docs WHERE id = 2;")
        statements.append(
            "INSERT INTO docs VALUES (%d,'c0','x','[%s]');"
            % (args.rows, probe)
        )
        query = (
            "SELECT id, L2_DISTANCE(embedding, '[%s]') AS distance FROM docs "
            "WHERE category = 'c3' ORDER BY distance LIMIT 10;" % probe
        )
        statements.append(query)
        statements.append("--restart")
        statements.append(query)
        statements.append("--mode ann")
        statements.extend([query] * args.repeat)

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
