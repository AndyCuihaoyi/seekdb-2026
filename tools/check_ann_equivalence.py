#!/usr/bin/env python3
"""Check that the approximate answer is the exact answer, byte for byte.

The pruning of the vector index is *exact*: a cell is skipped only when the
triangle inequality proves that no point of it can be closer than the
current K-th accepted distance, and the radius it uses is the radius of
that cell, measured over **every** row of the cell in the second pass of
the build.

That is a claim about the code, and this script is the evidence for it:
the same query is asked in the exact mode and in the approximate mode,
against the same data, and the two answers have to be identical - the same
identifiers, in the same order, with the same distances, down to the byte.

The claim is stronger than a recall: a recall of 0.95 tolerates a wrong
answer, this does not.

The script also covers the part of the build that changed: the centroids
are chosen from a bounded sample of the table, so the build is asked twice
on the same data and the two indexes have to be the same.

Usage:
    tools/check_ann_equivalence.py [--binary PATH] [--rows N] [--dim N]
                                   [--queries N] [--k N] [--seed N]
"""

import argparse
import json
import os
import random
import select
import shutil
import subprocess
import sys
import tempfile


def start(binary, data_dir):
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir
    env["MINIVECDB_MODE"] = "exact"
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError("the database closed its output: %s" % proc.stderr.read()[-1500:])
    return proc


def run(proc, sql, mode="exact", timeout=300.0):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError("the database did not answer %r" % sql)
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database exited: %s" % proc.stderr.read()[-1500:])
    return json.loads(line)


def close(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=30)
    except Exception:
        proc.kill()


def load(proc, rows, dim, categories):
    """Write the rows and build the vector index."""
    run(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim)
    batch = 100
    for first in range(0, len(rows), batch):
        values = []
        for row_id, cat, vector in rows[first : first + batch]:
            literal = "'[%s]'" % ",".join(repr(x) for x in vector)
            values.append("(%d,'%s',%s)" % (row_id, cat, literal))
        answer = run(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
        if answer.get("ok") is not True:
            raise RuntimeError("the insert failed: %r" % answer)
    answer = run(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
    if answer.get("ok") is not True:
        raise RuntimeError("the index was not created: %r" % answer)


def queries(rng, count, dim, k, categories):
    """The statements of the measurement, and the filters they use."""
    out = []
    for q in range(count):
        query = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
        literal = "'[%s]'" % ",".join(repr(x) for x in query)
        filtered = q % 2 == 1
        clause = " WHERE cat = 'c%d'" % (q % categories) if filtered else ""
        out.append(
            (
                "SELECT id, L2_DISTANCE(v, %s) AS d FROM t%s ORDER BY d LIMIT %d;"
                % (literal, clause, k),
                filtered,
            )
        )
    return out


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
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--categories", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    dim = args.dim
    rows = []
    for i in range(args.rows):
        vector = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
        rows.append((i, "c%d" % (i % args.categories), vector))

    work = tempfile.mkdtemp(prefix="mvdb-ann-eq-")
    data_dir = os.path.join(work, "data")
    second_dir = os.path.join(work, "data2")
    failures = 0
    try:
        statements = None
        approximate = None
        for attempt, directory in enumerate((data_dir, second_dir)):
            proc = start(args.binary, directory)
            try:
                load(proc, rows, dim, args.categories)
                statements = queries(random.Random(args.seed + 1000), args.queries,
                                     dim, args.k, args.categories)

                if attempt == 0:
                    # The same statements, asked twice: the answers have to
                    # be the same bytes.
                    exact = []
                    for sql, _filtered in statements:
                        answer = run(proc, sql, mode="exact")
                        if answer.get("ok") is not True:
                            raise RuntimeError("the exact query failed: %r" % answer)
                        exact.append(json.dumps(answer.get("rows"), sort_keys=True))

                    approximate = []
                    for sql, _filtered in statements:
                        answer = run(proc, sql, mode="ann")
                        if answer.get("ok") is not True:
                            raise RuntimeError("the approximate query failed: %r" % answer)
                        approximate.append(json.dumps(answer.get("rows"), sort_keys=True))

                    differing = 0
                    for index, (a, b) in enumerate(zip(exact, approximate)):
                        if a != b:
                            differing += 1
                            if differing <= 3:
                                print("FAIL  query %d differs" % index, file=sys.stderr)
                                print("  exact       %s" % a[:400], file=sys.stderr)
                                print("  approximate %s" % b[:400], file=sys.stderr)
                    if differing:
                        failures += 1
                        print("FAIL  %d of %d answers differ between the two modes"
                              % (differing, len(statements)), file=sys.stderr)
                    else:
                        print("ok    %d answers are identical in the exact and the "
                              "approximate mode" % len(statements))
                else:
                    # The build has to be the same every time: the centroids
                    # come from a reservoir sample, so the sample has to be
                    # the same too. The second build runs on a fresh data
                    # directory, so it is a build from scratch.
                    again = []
                    for sql, _filtered in statements:
                        answer = run(proc, sql, mode="ann")
                        again.append(json.dumps(answer.get("rows"), sort_keys=True))
                    if again != approximate:
                        failures += 1
                        print("FAIL  the second build of the index answers differently",
                              file=sys.stderr)
                    else:
                        print("ok    the second build of the index answers identically")
            finally:
                close(proc)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
