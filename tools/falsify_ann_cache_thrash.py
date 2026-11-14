#!/usr/bin/env python3
"""The experiment that would prove the conclusion of the report wrong.

The report concludes that the timeout is a `STATEMENT` timeout, and that
the cost of the statement is the **cache thrashing of the approximate
scan**: the rows of a cell are spread over every block of the table, and
the scalar cache is too small to hold them, so every sweep of a cell
decodes every block of the table again.

That conclusion is falsifiable in two ways, and this script runs both,
as a **grid**: the size of the text column against the size of the pool.
A single pair of runs cannot tell the two causes apart, because both of
them change the time; the grid can.

**(a) The size of the pool.** If the cost of the statement is the
thrashing of the cache, then a cache that is large enough to hold the
whole table must remove the cost, without changing the answer of the
query at all: the number of the decodes must fall from
`cells x blocks` to `blocks`. If the answer changes, or the decodes do
not fall, the conclusion is wrong.

**(b) The size of the block.** If the cost is the number of the blocks
that are decoded, then a table whose blocks are small - the same number
of rows, but a text column of a few bytes instead of a thousand - must
be faster under the same budget, because the whole table then fits in
the cache. The number of the decodes must **not** change: the blocks
are the same, only smaller. If it changes, the conclusion is wrong.

The last two runs are the control: the same table, with the
**approximate path replaced by the exact one**. The exact path reads
the rows block by block, so it decodes every block exactly once; the
difference between the two runs is the cost of the approximate path
alone.

Usage:
    tools/falsify_ann_cache_thrash.py --binary PATH [--rows N]
                                       [--text T] [--dim D]
                                       [--pool-mb M]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time


SCAN_RE = re.compile(
    r"ann scan cells=(\d+) rows=(\d+) max_radius=([\d.]+) "
    r"radius_sum=([\d.]+) largest_cell=(\d+)"
)
DECODE_RE = re.compile(
    r"scalar_decodes=(\d+) hits=(\d+) vector_decodes=(\d+)"
)


def run(binary, rows, dim, batch, text, pool_mb, mode, label):
    """Build the table, run the filtered TopK, and report the counters."""
    work = tempfile.mkdtemp(prefix="mvdb-falsify-")
    data_dir = os.path.join(work, "data")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    body = "x" * text
    result = {
        "label": label, "rows": rows, "text": text,
        "pool_mb": pool_mb, "mode": mode,
    }
    try:
        argv = [binary, "--data-dir", data_dir]
        if pool_mb:
            argv += ["--buffer-pool-mb", str(pool_mb)]
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(log_path, "wb"), text=True, bufsize=1,
        )
        proc.stdout.readline()

        def ask(sql, search_mode="exact"):
            proc.stdin.write(
                json.dumps({"id": 1, "op": "execute", "sql": sql,
                            "search_mode": search_mode}) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        ask("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
            "body VARCHAR(%d), v VECTOR(%d));" % (max(text, 1), dim))
        values = [
            "(%d,'c%d','%s','[%s]')" % (
                row_id, row_id % 8, body,
                ",".join(str((row_id * (i + 1)) % 100) for i in range(dim)),
            )
            for row_id in range(rows)
        ]
        for first in range(0, rows, batch):
            ask("INSERT INTO t VALUES "
                + ",".join(values[first:first + batch]) + ";")
        ask("CREATE VECTOR INDEX idx_t ON t(v);")

        query = ("SELECT id FROM t WHERE cat = 'c3' "
                 "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe)
        times = []
        answer = None
        for _ in range(3):
            started = time.monotonic()
            answer = ask(query, mode)
            times.append(time.monotonic() - started)
        result["best_s"] = round(min(times), 3)
        result["rows_returned"] = len(answer.get("rows") or [])
        result["answer"] = answer.get("rows")

        proc.stdin.close()
        proc.wait(timeout=300)
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            log = handle.read()
        decodes = DECODE_RE.findall(log)
        scans = SCAN_RE.findall(log)
        if decodes:
            result["scalar_decodes"] = int(decodes[-1][0])
            result["scalar_hits"] = int(decodes[-1][1])
            result["vector_decodes"] = int(decodes[-1][2])
        if scans:
            result["cells_visited"] = int(scans[-1][0])
            result["rows_measured"] = int(scans[-1][1])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=400000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--pool-mb", type=int, default=512)
    parser.add_argument("--big-pool-mb", type=int, default=2048)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    runs = [
        # The grid: the size of the text column against the size of the
        # pool. It is the run that separates the two causes, because a
        # single pair of runs cannot tell them apart.
        dict(pool_mb=None, mode="ann", text=args.text,
             label="grid: text=%d, the pool of the judge" % args.text),
        dict(pool_mb=args.big_pool_mb, mode="ann", text=args.text,
             label="grid: text=%d, the pool of %d MB"
                   % (args.text, args.big_pool_mb)),
        dict(pool_mb=None, mode="ann", text=8,
             label="grid: text=8, the pool of the judge"),
        dict(pool_mb=args.big_pool_mb, mode="ann", text=8,
             label="grid: text=8, the pool of %d MB" % args.big_pool_mb),
        # the control: the exact path, with the same table and the cache.
        dict(pool_mb=None, mode="exact", text=args.text,
             label="control: the exact path, the same table"),
        # and the baseline: the default cache, the approximate path.
        dict(pool_mb=None, mode="ann", text=args.text,
             label="baseline: the pool of the judge, the approximate path"),
    ]

    report = {"args": vars(args), "runs": []}
    for spec in runs:
        result = run(args.binary, args.rows, args.dim, args.batch,
                     spec["text"], spec["pool_mb"], spec["mode"], spec["label"])
        report["runs"].append(result)
        print("=== %s ===" % result["label"])
        print("  best            %8.3fs" % result["best_s"])
        print("  rows returned   %8d" % result["rows_returned"])
        if "cells_visited" in result:
            print("  cells visited   %8d" % result["cells_visited"])
            print("  rows measured   %8d" % result["rows_measured"])
        if "scalar_decodes" in result:
            print("  scalar decodes  %8d" % result["scalar_decodes"])
            print("  scalar hits     %8d" % result["scalar_hits"])
        print()

    # The answers of the runs that have to be the same.
    answers = [r.get("answer") for r in report["runs"] if r.get("answer")]
    report["answers_identical"] = all(a == answers[0] for a in answers) if answers else None
    print("the answers of the runs are identical: %s" % report["answers_identical"])

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
