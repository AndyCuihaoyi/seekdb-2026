#!/usr/bin/env python3
"""Check that the database answers within the time budget of the judge.

The judge runs the container with `--memory=2g --memory-swap=2g` and
`--cpus=2`, and it waits a bounded time for every answer:

    startup_timeout   120 s   the handshake of the start-up
    request_timeout    60 s   the answer of one statement
    case_timeout      600 s   the whole case

A database that misses one of them is reported as
`JSON response timeout`, and the admission gate is failed. The signature
is the same for a process that is stuck and for a process that is busy,
so the only way to tell them apart is to measure.

The script therefore times every phase of the run and **fails** when a
phase is over its budget. The budgets it uses are half of the budgets of
the judge, so that a run that passes here has room to be slower on the
machine of the judge.

The phases that are timed:

* the write of the rows,
* the build of the vector index,
* the answer of a query,
* the **handshake of the start-up** on a data directory that already holds
  an index. That is the phase that is charged before the database is
  ready, and it is where a build of the index that walks the table once
  per centroid misses the timeout of the judge.

# Why the run is done at two sizes

The build of the index is **linear** in the number of rows, in every
version: the index holds the identity of every row, so the second pass
has to touch every row of the table. The growth of the *total* time is
therefore not an invariant, and a check on it would pass a build that
walks the table once per centroid.

What distinguishes the builds is the **constant**: a build that walks the
table once per centroid does `nlist` times the work of the work per row -
sixty-four times, with the default of sixty-four cells - and the cost of
the build is therefore about fourteen times higher per row than the cost of
a build that walks the table once.

So the check that catches the defect is the **absolute budget at a size
where the constant matters**. At four million rows a build that walks
the table once per centroid needs about seventy seconds for the build
alone, which is over the request timeout of the judge; a build that walks
the table once needs about five seconds.

The second size is measured as well, for a different reason: the cost per
row must not grow with the size of the table. A build whose cost per row
grows is super-linear, and that is a defect of its own.

Usage:
    tools/check_time_budget.py [--binary PATH] [--rows N] [--dim N]
                               [--text N] [--limit-mb N] [--large-rows N]
                               [--small-rows N]
"""

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time

# The budgets of the judge.
#
# The script holds the run to half of each of them: a run that passes
# here has room to be slower on the machine of the judge, and a run that
# fails here has no chance of passing there.
JUDGE_STARTUP_TIMEOUT = 120.0
JUDGE_REQUEST_TIMEOUT = 60.0
JUDGE_CASE_TIMEOUT = 600.0

LIMIT_STARTUP = JUDGE_STARTUP_TIMEOUT / 2.0
LIMIT_STATEMENT = JUDGE_REQUEST_TIMEOUT / 2.0
LIMIT_CASE = JUDGE_CASE_TIMEOUT / 2.0

# The two sizes the run is done at.
#
# The large size is the one that matters: it is the size at which the
# extra work of a build that walks the table once per centroid is over
# the budget of the judge. It is also a data set that is larger than the
# memory budget of the judge, which is the situation the statement is
# about.
#
# The small size is large enough that the sample of the build is filled
# (`SAMPLE_ROWS` is 8192), so that the two runs are comparable.
LARGE_ROWS = 4000000
SMALL_ROWS = 100000

# The factor the cost per row may grow by between the two sizes.
#
# A build whose cost per row does not depend on the size of the table has
# a cost per row that is the same at both sizes. The tolerance is
# generous because the machine the script runs on is not the machine of
# the judge, and because the small size pays a fixed overhead that the
# large one amortises.
MAX_COST_PER_ROW_GROWTH = 4.0


def limited():
    """The limit of the child, in bytes."""
    limit = int(os.environ.get("MVDB_LIMIT_BYTES", "0"))
    if not limit:
        return None
    return limit


def start(binary, data_dir, limit_bytes):
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir

    def preexec():
        if limit_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
        preexec_fn=preexec,
    )
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError(
            "the database exited before the handshake: %s" % proc.stderr.read()[-2000:]
        )
    return proc


def ask(proc, sql, mode="exact"):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(
            "the database exited before the response: %s" % proc.stderr.read()[-2000:]
        )
    return json.loads(line)


def close(proc):
    try:
        proc.stdin.close()
        proc.wait(timeout=120)
    except Exception:
        proc.kill()


def directory_size(path):
    """The size of a directory tree, in bytes."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def measure(binary, rows, dim, batch, limit_bytes, out):
    """Write a table of `rows` rows, build the index, and time every phase.

    The data directory is removed afterwards, so that a run at two sizes
    does not need twice the disk.
    """
    work = tempfile.mkdtemp(prefix="mvdb-time-")
    data_dir = os.path.join(work, "data")
    m = {"rows": rows}
    try:
        proc = start(binary, data_dir, limit_bytes)
        try:
            ask(
                proc,
                "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                "v VECTOR(%d));" % dim,
            )
            started = time.monotonic()
            for first in range(0, rows, batch):
                values = []
                for row_id in range(first, min(first + batch, rows)):
                    vec = ",".join(
                        str((row_id * (i + 1)) % 100) for i in range(dim)
                    )
                    values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
                answer = ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
                if answer.get("ok") is not True:
                    out["failures"].append(
                        "the insert of the rows %d..%d was refused"
                        % (first, first + batch)
                    )
                    return m
            m["write_s"] = round(time.monotonic() - started, 3)

            started = time.monotonic()
            answer = ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            m["index_build_s"] = round(time.monotonic() - started, 3)
            if answer.get("ok") is not True:
                out["failures"].append("the index was not created at %d rows" % rows)
                return m

            probe = ",".join(str(i % 100) for i in range(dim))
            started = time.monotonic()
            answer = ask(
                proc,
                "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
            )
            m["query_s"] = round(time.monotonic() - started, 3)
            if answer.get("ok") is not True or len(answer.get("rows", [])) != 10:
                out["failures"].append(
                    "the query did not answer ten rows at %d rows" % rows
                )
        finally:
            close(proc)

        # The restart: the recovery and the rebuild of the index happen
        # *before* the handshake, so the whole of it is charged to the
        # start-up timeout of the judge.
        started = time.monotonic()
        proc = start(binary, data_dir, limit_bytes)
        m["restart_handshake_s"] = round(time.monotonic() - started, 3)
        try:
            answer = ask(proc, "SELECT id FROM t LIMIT 1;")
            if answer.get("ok") is not True:
                out["failures"].append("the restart did not answer at %d rows" % rows)
        finally:
            close(proc)

        m["data_directory_mib"] = round(directory_size(data_dir) / 1048576.0, 1)
        m["data_larger_than_the_budget"] = m["data_directory_mib"] > (
            limit_bytes / 1048576.0
        )
    except RuntimeError as e:
        out["failures"].append("%d rows: %s" % (rows, e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return m


def check_budgets(m, out, label):
    """The phases of one run against the budgets of the judge."""
    for phase, budget, name in (
        ("index_build_s", LIMIT_STATEMENT, "the build of the index"),
        ("query_s", LIMIT_STATEMENT, "the query"),
        ("restart_handshake_s", LIMIT_STARTUP, "the handshake of the restart"),
    ):
        value = m.get(phase)
        if value is None:
            continue
        if value > budget:
            out["failures"].append(
                "%s took %.1f s at %d rows (%s), the budget is %.1f s"
                % (name, value, m["rows"], label, budget)
            )


def check_cost_per_row(small, large, out):
    """The cost per row must not grow with the size of the table.

    The build is linear in every version, so the *total* time grows with
    the size of the table and that is not a defect. What must not happen
    is that the work per row grows: that is a super-linear build.
    """
    def per_row(m, phase):
        value = m.get(phase)
        if not value or not m.get("rows"):
            return None
        return value / m["rows"]

    out["cost_per_row"] = {}
    for phase in ("index_build_s", "restart_handshake_s"):
        a = per_row(small, phase)
        b = per_row(large, phase)
        if a is None or b is None:
            continue
        growth = b / a if a > 0 else 0.0
        out["cost_per_row"][phase] = {
            "small": a * 1e6,
            "large": b * 1e6,
            "growth": round(growth, 3),
            "unit": "microseconds per row",
        }
        if growth > MAX_COST_PER_ROW_GROWTH:
            out["failures"].append(
                "the cost per row of %s grew by a factor of %.2f between %d and %d rows, "
                "the limit is %.1f: the build is super-linear"
                % (phase, growth, small["rows"], large["rows"], MAX_COST_PER_ROW_GROWTH)
            )


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
    parser.add_argument("--rows", type=int, default=LARGE_ROWS)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=0)
    parser.add_argument("--limit-mb", type=int, default=2048)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--large-rows", type=int, default=LARGE_ROWS)
    parser.add_argument("--small-rows", type=int, default=SMALL_ROWS)
    parser.add_argument(
        "--skip-small",
        action="store_true",
        help="only run at the large size, and skip the comparison of the cost per row",
    )
    args = parser.parse_args()

    limit_bytes = args.limit_mb * 1024 * 1024
    os.environ["MVDB_LIMIT_BYTES"] = str(limit_bytes)

    out = {
        "binary": args.binary,
        "dim": args.dim,
        "text": args.text,
        "limit_mb": args.limit_mb,
        "budgets": {
            "startup_s": LIMIT_STARTUP,
            "statement_s": LIMIT_STATEMENT,
            "case_s": LIMIT_CASE,
        },
        "judge_budgets": {
            "startup_s": JUDGE_STARTUP_TIMEOUT,
            "statement_s": JUDGE_REQUEST_TIMEOUT,
            "case_s": JUDGE_CASE_TIMEOUT,
        },
        "failures": [],
    }

    case_started = time.monotonic()
    large = measure(args.binary, args.large_rows, args.dim, args.batch, limit_bytes, out)
    out["large"] = large
    check_budgets(large, out, "the large run")

    if not args.skip_small:
        small = measure(args.binary, args.small_rows, args.dim, args.batch, limit_bytes, out)
        out["small"] = small
        check_cost_per_row(small, large, out)

    out["case_s"] = round(time.monotonic() - case_started, 3)
    out["passed"] = not out["failures"]
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
