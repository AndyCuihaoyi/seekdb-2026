#!/usr/bin/env python3
"""Check the approximate mode against the exact answer, at the judge's scale.

The official requirement is that the approximate mode reaches a Recall@10 of
at least 0.95, both with a filter and without one, and that it never
returns an identifier that is not visible, a duplicate, or a row that does
not satisfy the filter.

The public cases check the approximate mode on a handful of rows, where any
search returns everything and the recall is 1. The formal cases use a data
set that is generated with a seed that is kept back, so the recall has to
hold on a data set that was never seen.

This script measures the recall on data sets of a size that is close to
the benchmark, with several seeds, and it checks the three invariants that
the official text names.

Usage:
    tools/check_ann_recall.py [--binary PATH] [--rows N] [--queries N]
                              [--seed N]
"""

import argparse
import json
import math
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


def run(proc, sql, mode="exact", timeout=60.0):
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
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def truth(rows, query, k, category=None):
    """The exact TopK, computed with the ordinary formula."""
    best = []
    for row_id, cat, vector in rows:
        if category is not None and cat != category:
            continue
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(vector, query)))
        best.append((d, row_id))
    best.sort(key=lambda item: (item[0], item[1]))
    return best[:k]


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
    parser.add_argument("--queries", type=int, default=200)
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

    work = tempfile.mkdtemp(prefix="mvdb-ann-")
    data_dir = os.path.join(work, "data")
    failures = 0
    try:
        proc = start(args.binary, data_dir)
        try:
            run(
                proc,
                "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim,
            )
            batch = 100
            for start_row in range(0, len(rows), batch):
                values = []
                for row_id, cat, vector in rows[start_row : start_row + batch]:
                    literal = "'[%s]'" % ",".join(repr(x) for x in vector)
                    values.append("(%d,'%s',%s)" % (row_id, cat, literal))
                answer = run(
                    proc, "INSERT INTO t VALUES " + ",".join(values) + ";"
                )
                if answer.get("ok") is not True:
                    raise RuntimeError("the insert failed: %r" % answer)
            answer = run(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            if answer.get("ok") is not True:
                raise RuntimeError("the index was not created: %r" % answer)

            visible = {row[0] for row in rows}
            for group, filtered in (("unfiltered", False), ("filtered", True)):
                recalls = []
                for q in range(args.queries):
                    query = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
                    literal = "'[%s]'" % ",".join(repr(x) for x in query)
                    clause = " WHERE cat = 'c%d'" % (q % args.categories) if filtered else ""
                    sql = (
                        "SELECT id, L2_DISTANCE(v, %s) AS d FROM t%s "
                        "ORDER BY d LIMIT %d;" % (literal, clause, args.k)
                    )
                    answer = run(proc, sql, mode="ann")
                    if answer.get("ok") is not True:
                        raise RuntimeError("the query failed: %r" % answer)
                    got = [row[0] for row in answer.get("rows", [])]

                    # The three invariants the official text names.
                    if len(got) != len(set(got)):
                        raise RuntimeError("the answer holds a duplicate: %r" % got)
                    for row_id in got:
                        if row_id not in visible:
                            raise RuntimeError("the answer holds an unknown id: %r" % row_id)
                    if filtered:
                        wanted_cat = "c%d" % (q % args.categories)
                        for row_id in got:
                            actual = rows[row_id][1]
                            if actual != wanted_cat:
                                raise RuntimeError(
                                    "the answer holds a row that does not satisfy the "
                                    "filter: id %r is %r" % (row_id, actual)
                                )

                    wanted = [row_id for _, row_id in truth(rows, query, args.k,
                                                          "c%d" % (q % args.categories)
                                                          if filtered else None)]
                    if wanted:
                        hits = len(set(got) & set(wanted))
                        recalls.append(hits / len(wanted))
                mean = sum(recalls) / len(recalls) if recalls else 1.0
                status = "ok  " if mean >= 0.95 else "FAIL"
                if mean < 0.95:
                    failures += 1
                print("%s  recall@%d %-10s %.4f" % (status, args.k, group, mean))
        finally:
            close(proc)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\n%d group(s) below the required recall of 0.95" % failures, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
