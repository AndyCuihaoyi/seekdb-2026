#!/usr/bin/env python3
"""Read the diagnostics of the index out of the log of the database.

The database prints the diagnostics of the approximate scan to stderr:
the number of cells the scan visited, the number of rows it measured,
the largest radius of a cell and the sum of the radii. They are the
numbers that decide the cost of the scan, so they are what a change of
the centroids is measured with.

This script runs one approximate query and reads the numbers back out
of the log of the process.

Usage:
    tools/probe_scan_stats.py --binary PATH [--rows N] [--dim D]
                               [--text T] [--cpus N]
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

sys.path.insert(0, "/tmp/official/protocol_check")

from _transport import Session  # noqa: E402


CONFIG_PATH = "/tmp/official/protocol_check/config.json"
STATS_RE = re.compile(
    r"ann scan cells=(\d+) rows=(\d+) max_radius=([\d.]+) "
    r"radius_sum=([\d.]+) largest_cell=(\d+)"
)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def run(config, binary, rows, dim, batch, cpus, text):
    work = tempfile.mkdtemp(prefix="mvdb-stats-")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    body = "x" * text if text else ""
    result = {"rows": rows, "text": text}
    try:
        session = Session(
            config, log_path,
            command=["taskset", "-c", "0,%d" % (cpus - 1), "sh", "-c",
                     "exec %s --data-dir {data_dir}" % binary],
            case_timeout=3600,
        )
        try:
            session.start()
            if text:
                ddl = ("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                       "body VARCHAR(%d), v VECTOR(%d));" % (text, dim))
            else:
                ddl = ("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                       "v VECTOR(%d));" % dim)
            session.request(ddl, "exact")
            for first in range(0, rows, batch):
                values = []
                for row_id in range(first, min(first + batch, rows)):
                    vec = ",".join(
                        str((row_id * (i + 1)) % 100) for i in range(dim)
                    )
                    if text:
                        values.append(
                            "(%d,'c%d','%s','[%s]')"
                            % (row_id, row_id % 8, body, vec)
                        )
                    else:
                        values.append(
                            "(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec)
                        )
                session.request(
                    "INSERT INTO t VALUES " + ",".join(values) + ";", "exact"
                )
            session.request("CREATE VECTOR INDEX idx_t ON t(v);", "exact")

            started = time.monotonic()
            session.request(
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe, "ann",
            )
            result["ann_filtered_s"] = round(time.monotonic() - started, 3)

            started = time.monotonic()
            session.request(
                "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "ann",
            )
            result["ann_unfiltered_s"] = round(time.monotonic() - started, 3)
        finally:
            try:
                session.shutdown()
            except Exception:
                pass
            session.close()
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            text_log = handle.read()
        stats = []
        for match in STATS_RE.finditer(text_log):
            stats.append({
                "cells": int(match.group(1)),
                "rows_measured": int(match.group(2)),
                "max_radius": float(match.group(3)),
                "radius_sum": float(match.group(4)),
                "largest_cell": int(match.group(5)),
            })
        result["scans"] = stats
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", action="append", required=True)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=0)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    report = {"args": vars(args), "runs": []}
    for binary in args.binary:
        result = run(
            config, binary, args.rows, args.dim, args.batch, args.cpus, args.text
        )
        report["runs"].append(result)
        for scan in result["scans"]:
            print(
                "%-40s cells=%-4d rows=%-9d max_radius=%9.4f "
                "radius_sum=%10.4f largest_cell=%d"
                % (
                    os.path.basename(binary),
                    scan["cells"],
                    scan["rows_measured"],
                    scan["max_radius"],
                    scan["radius_sum"],
                    scan["largest_cell"],
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
