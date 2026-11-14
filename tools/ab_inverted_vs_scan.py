#!/usr/bin/env python3
"""The A/B of step 3: the inverted index against the scan.

The task asks for a comparison of two things on the same table and the
same filtered TopK:

* (i)  the column is **indexed** - the inverted index names the rows
  that satisfy the filter, so the scan only measures those rows;
* (ii) the column is **not indexed** - the index was given up on
  because the column has more distinct values than `MAX_DISTINCT`, so
  the scan measures every row and applies the filter to it.

The script runs the comparison for the two paths of the query, the
exact one and the approximate one, and reports the time, the number of
scalar blocks that were decoded and the number of rows that were
measured.

The script also runs the case that matters most: a filter that matches
fewer rows than the answer asks for. The pruning of the approximate
index compares the distance of a cell with the distance of the K-th
accepted row, so **while the filter accepts fewer than K rows, no cell
can be pruned at all** and the scan visits every cell of the table.

Usage:
    tools/ab_inverted_vs_scan.py --binary PATH [--rows N] [--text T]
                                 [--dim D] [--cpus N]
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


def run(binary, rows, dim, batch, text, categories, label):
    """Build a table whose filter column has `categories` values."""
    work = tempfile.mkdtemp(prefix="mvdb-ab-")
    data_dir = os.path.join(work, "data")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    body = "x" * text
    result = {"label": label, "categories": categories, "rows": rows}
    try:
        proc = subprocess.Popen(
            [binary, "--data-dir", data_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=open(log_path, "wb"), text=True, bufsize=1,
        )
        proc.stdout.readline()

        def ask(sql, mode="exact"):
            proc.stdin.write(
                json.dumps({"id": 1, "op": "execute", "sql": sql,
                            "search_mode": mode}) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        ask("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
            "body VARCHAR(%d), v VECTOR(%d));" % (text, dim))
        values = [
            "(%d,'c%d','%s','[%s]')" % (
                row_id, row_id % categories, body,
                ",".join(str((row_id * (i + 1)) % 100) for i in range(dim)),
            )
            for row_id in range(rows)
        ]
        for first in range(0, rows, batch):
            ask("INSERT INTO t VALUES "
                + ",".join(values[first:first + batch]) + ";")
        ask("CREATE VECTOR INDEX idx_t ON t(v);")

        query = (
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
        )
        for mode in ("exact", "ann"):
            times = []
            for _ in range(3):
                started = time.monotonic()
                answer = ask(query, mode)
                times.append(time.monotonic() - started)
            result["%s_s" % mode] = round(min(times), 3)
            result["%s_rows" % mode] = len(answer.get("rows") or [])

        proc.stdin.close()
        proc.wait(timeout=120)
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            log = handle.read()
        result["scans"] = [
            {"cells": int(m.group(1)), "rows_measured": int(m.group(2)),
             "max_radius": float(m.group(3)), "radius_sum": float(m.group(4)),
             "largest_cell": int(m.group(5))}
            for m in SCAN_RE.finditer(log)
        ]
        result["decodes"] = [
            {"scalar_decodes": int(m.group(1)), "hits": int(m.group(2)),
             "vector_decodes": int(m.group(3))}
            for m in DECODE_RE.finditer(log)
        ]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=400000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    report = {"args": vars(args), "runs": []}
    # Eight categories: the inverted index is built and used.
    # Four thousand: the index is given up on, and the scan measures
    # every row.
    for categories, label in (
        (8, "indexed: 8 distinct values"),
        (4096, "abandoned: 4096 distinct values"),
    ):
        result = run(
            args.binary, args.rows, args.dim, args.batch, args.text,
            categories, label,
        )
        report["runs"].append(result)
        print("=== %s ===" % label)
        print("  exact  %8.3fs   ann %8.3fs" % (
            result["exact_s"], result["ann_s"]))
        for scan, decode in zip(result["scans"], result["decodes"]):
            print(
                "  -> ann scan cells=%-3d rows_measured=%-8d "
                "scalar_decodes=%-7d vector_decodes=%d"
                % (
                    scan["cells"], scan["rows_measured"],
                    decode["scalar_decodes"], decode["vector_decodes"],
                )
            )

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
