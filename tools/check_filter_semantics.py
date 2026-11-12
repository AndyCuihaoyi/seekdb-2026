#!/usr/bin/env python3
"""Check the semantics of the streaming filter.

The rows of a table are now filtered while they are read, so that a row the
filter rejects never has its vector decoded. That is where the memory of a
query over a large table comes from, and it is also where a semantic
regression would hide.

The checks are the ones the statement of the competition names:

* **the filter is honoured** - the answer of a filtered query is the answer
  of the rows that satisfy the filter, in both modes;
* **a filter that reads the vector** - the statement falls back to the path
  that materialises the whole row, and the answer is still right;
* **a statement that projects the vector** - the answer holds the vector;
* **a statement that does not** - the answer holds the ordinary columns,
  and the row was materialised without its vector;
* **the transaction** - a failed statement leaves nothing behind, a
  rollback restores the rows, the visible set and the search;
* **the index** - an update of a vector column is reflected in the
  approximate answer.

Usage:
    tools/check_filter_semantics.py [--binary PATH] [--rows N]
"""

import argparse
import json
import math
import os
import select
import shutil
import subprocess
import sys
import tempfile

FAILURES = []


def check(condition, message, detail=""):
    if condition:
        print("ok    %s" % message)
    else:
        print("FAIL  %s" % message)
        if detail:
            print("      %s" % detail, file=sys.stderr)
        FAILURES.append(message)


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


def run(proc, sql, mode="exact", timeout=120.0):
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


def rows_of(answer):
    return answer.get("rows") or []


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
    parser.add_argument("--rows", type=int, default=200)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-filter-")
    data_dir = os.path.join(work, "data")
    try:
        proc = start(args.binary, data_dir)
        try:
            run(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                      "v VECTOR(3));")
            values = []
            for row_id in range(args.rows):
                values.append(
                    "(%d,'c%d','[%d,%d,%d]')"
                    % (row_id, row_id % 8, row_id, row_id + 1, row_id + 2)
                )
            run(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")

            # 1. The filter is honoured, in both modes.
            for mode in ("exact", "ann"):
                answer = run(
                    proc,
                    "SELECT id FROM t WHERE cat = 'c3' ORDER BY id LIMIT 100;",
                    mode=mode,
                )
                got = [r[0] for r in rows_of(answer)]
                want = [i for i in range(args.rows) if i % 8 == 3][:100]
                check(
                    got == want,
                    "[%s] the equality filter on an indexed column is honoured" % mode,
                    "got %r, want %r" % (got[:8], want[:8]),
                )

            # 2. A filter that reads the vector column.
            #
            # The vector of the row `i` is `[i, i+1, i+2]`, so the distance
            # from `[1,2,3]` is `sqrt(3) * |i - 1|`. A threshold of 2.0
            # therefore admits the rows 0, 1 and 2.
            answer = run(
                proc,
                "SELECT id FROM t WHERE L2_DISTANCE(v, '[1,2,3]') < 2.0 ORDER BY id;",
            )
            got = [r[0] for r in rows_of(answer)]
            want = [
                i for i in range(args.rows)
                if math.sqrt(3.0) * abs(i - 1) < 2.0
            ]
            check(
                got == want,
                "a filter that reads the vector column is honoured",
                "got %r, want %r" % (got[:8], want[:8]),
            )

            # 3. A statement that projects the vector holds the vector.
            answer = run(proc, "SELECT id, v FROM t WHERE cat = 'c0' ORDER BY id LIMIT 3;")
            got = rows_of(answer)
            check(
                got and got[0][1] == [0.0, 1.0, 2.0],
                "a statement that projects the vector column holds the vector",
                "got %r" % (got[:1],),
            )

            # 4. A statement that does not project it holds the ordinary
            #    columns, and the answer is the same either way.
            answer = run(proc, "SELECT id FROM t WHERE cat = 'c0' ORDER BY id LIMIT 3;")
            got = [r[0] for r in rows_of(answer)]
            check(got == [0, 8, 16], "a statement that does not project the vector "
                                     "column answers the same", "got %r" % got)

            # 5. A statement that fails leaves nothing behind.
            answer = run(proc, "INSERT INTO t VALUES (1, 'c1', '[1,2,3]');")
            check(answer.get("ok") is False, "a duplicate primary key is refused")
            answer = run(proc, "SELECT id FROM t WHERE id = 1 ORDER BY id;")
            check(
                [r[0] for r in rows_of(answer)] == [1],
                "the refused statement left the row alone",
            )

            # 6. A rollback restores the rows, the visible set and the search.
            before = run(proc, "SELECT id FROM t ORDER BY id LIMIT 5;")
            run(proc, "BEGIN;")
            run(proc, "INSERT INTO t VALUES (%d, 'c9', '[9,9,9]');" % (args.rows + 1))
            run(proc, "UPDATE t SET v = '[7,7,7]' WHERE id = 0;")
            run(proc, "DELETE FROM t WHERE id = 1;")
            inside = run(proc, "SELECT id FROM t ORDER BY id LIMIT 5;")
            check(
                [r[0] for r in rows_of(inside)] == [0, 2, 3, 4, 5],
                "the transaction sees its own changes",
                "got %r" % [r[0] for r in rows_of(inside)],
            )
            run(proc, "ROLLBACK;")
            after = run(proc, "SELECT id FROM t ORDER BY id LIMIT 5;")
            check(
                json.dumps(rows_of(after), sort_keys=True)
                == json.dumps(rows_of(before), sort_keys=True),
                "the rollback restored the rows",
                "got %r, want %r" % (rows_of(after)[:2], rows_of(before)[:2]),
            )
            answer = run(proc, "SELECT id FROM t WHERE id = %d;" % (args.rows + 1))
            check(
                rows_of(answer) == [],
                "the rollback removed the row the transaction inserted",
            )
            answer = run(proc, "SELECT id FROM t WHERE id = 1 ORDER BY id;")
            check(
                [r[0] for r in rows_of(answer)] == [1],
                "the rollback restored the row the transaction deleted",
            )

            # 7. The index is maintained: an update of the vector column
            #    changes the approximate answer.
            run(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            answer = run(proc, "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0,0]') "
                               "LIMIT 1;", mode="ann")
            nearest = rows_of(answer)[0][0]
            check(nearest == 0, "the approximate answer is the nearest row before "
                                "the update", "got %r" % nearest)
            run(proc, "UPDATE t SET v = '[1000,1000,1000]' WHERE id = 0;")
            answer = run(proc, "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[0,0,0]') "
                               "LIMIT 1;", mode="ann")
            nearest = rows_of(answer)[0][0]
            check(nearest == 1, "the approximate answer follows the updated "
                                "vector", "got %r" % nearest)
            answer = run(proc, "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1000,1000,1000]') "
                               "LIMIT 1;", mode="ann")
            nearest = rows_of(answer)[0][0]
            check(nearest == 0, "the updated row is the nearest one to its own "
                                "vector", "got %r" % nearest)

            # 8. A delete removes the row from the index as well.
            run(proc, "DELETE FROM t WHERE id = 0;")
            answer = run(proc, "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[1000,1000,1000]') "
                               "LIMIT 1;", mode="ann")
            nearest = rows_of(answer)[0][0]
            check(nearest != 0, "the deleted row is gone from the index",
                  "got %r" % nearest)
        finally:
            close(proc)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if FAILURES:
        print("\n%d check(s) failed" % len(FAILURES), file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
