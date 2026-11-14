#!/usr/bin/env python3
"""Compare two builds of the database on the same workload, side by side.

The two builds are the same program at two commits, so a difference of
their times is a difference of their behaviour and not of their
environment. The script writes the same data to both, runs the same
statements, and reports the time of every statement next to the
diagnostics the database printed for it.

Usage:
    tools/compare_ann_side_by_side.py --a PATH --b PATH [--rows N]
                                       [--text T] [--dim D] [--cpus N]
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


DECODE_RE = re.compile(r"scalar_decodes=(\d+) vector_decodes=(\d+)")
SCAN_RE = re.compile(
    r"ann scan cells=(\d+) rows=(\d+) max_radius=([\d.]+) "
    r"radius_sum=([\d.]+) largest_cell=(\d+)"
)


def run(binary, rows, dim, batch, text, work):
    """Write the data, build the index, and time the queries."""
    data_dir = os.path.join(work, "data")
    os.makedirs(data_dir, exist_ok=True)
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    body = "x" * text
    result = {"binary": binary}

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

    started = time.monotonic()
    ask("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
        "body VARCHAR(%d), v VECTOR(%d));" % (text, dim))
    values = []
    for row_id in range(rows):
        vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(dim))
        values.append(
            "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
        )
    for first in range(0, rows, batch):
        ask("INSERT INTO t VALUES " + ",".join(values[first:first + batch]) + ";")
    result["write_s"] = round(time.monotonic() - started, 3)

    started = time.monotonic()
    ask("CREATE VECTOR INDEX idx_t ON t(v);")
    result["index_build_s"] = round(time.monotonic() - started, 3)

    query = (
        "SELECT id FROM t WHERE cat = 'c3' "
        "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
    )
    for mode in ("exact", "ann"):
        times = []
        for _ in range(3):
            started = time.monotonic()
            ask(query, mode)
            times.append(time.monotonic() - started)
        result["%s_s" % mode] = round(min(times), 3)
        result["%s_s_all" % mode] = [round(t, 3) for t in times]

    proc.stdin.close()
    proc.wait(timeout=120)
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        log = handle.read()
    result["decodes"] = [
        {"scalar": int(m.group(1)), "vector": int(m.group(2))}
        for m in DECODE_RE.finditer(log)
    ]
    result["scans"] = [
        {"cells": int(m.group(1)), "rows_measured": int(m.group(2)),
         "max_radius": float(m.group(3)), "radius_sum": float(m.group(4)),
         "largest_cell": int(m.group(5))}
        for m in SCAN_RE.finditer(log)
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a", required=True)
    parser.add_argument("--b", required=True)
    parser.add_argument("--rows", type=int, default=400000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    report = {"args": vars(args), "runs": []}
    for label, binary in (("A", args.a), ("B", args.b)):
        work = tempfile.mkdtemp(prefix="mvdb-sbs-")
        try:
            result = run(binary, args.rows, args.dim, args.batch, args.text, work)
            result["label"] = label
            report["runs"].append(result)
            print("=== %s: %s ===" % (label, os.path.basename(binary)))
            print("  write        %8.3fs" % result["write_s"])
            print("  index build  %8.3fs" % result["index_build_s"])
            print("  exact query  %8.3fs" % result["exact_s"])
            print("  ann query    %8.3fs" % result["ann_s"])
            for scan, decode in zip(result["scans"], result["decodes"]):
                print(
                    "  -> ann scan cells=%-3d rows=%-8d scalar_decodes=%-7d "
                    "vector_decodes=%-7d max_radius=%.2f"
                    % (
                        scan["cells"], scan["rows_measured"],
                        decode["scalar"], decode["vector"],
                        scan["max_radius"],
                    )
                )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    if len(report["runs"]) == 2:
        a, b = report["runs"]
        report["ratio_ann"] = round(b["ann_s"] / max(a["ann_s"], 1e-9), 2)
        report["ratio_exact"] = round(b["exact_s"] / max(a["exact_s"], 1e-9), 2)
        print()
        print("B/A  ann: %.2fx   exact: %.2fx"
              % (report["ratio_ann"], report["ratio_exact"]))

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
