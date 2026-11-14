#!/usr/bin/env python3
"""Measure the approximate query with a filter of every selectivity.

The approximate scan prunes a cell by the distance of its **K-th
accepted row**. While the filter accepts fewer than K rows, there is no
K-th accepted row, so **no cell can be pruned**: the scan visits every
cell and measures every row of the table.

A filter that matches few rows is therefore the worst case of the
approximate path, and the case the judge is most likely to hold: the
statement asks for a filtered TopK, and the filter is narrow.

This script measures the query for a series of selectivities - one row
in 8, one in 64, one in 512, one in 4096 - and reports the time, the
number of cells the scan visited and the number of rows it measured.

Usage:
    tools/probe_ann_selectivity.py --binary PATH [--rows N] [--text T]
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

sys.path.insert(0, "/tmp/official/protocol_check")

from _transport import Session  # noqa: E402


CONFIG_PATH = "/tmp/official/protocol_check/config.json"
STATS_RE = re.compile(
    r"ann scan cells=(\d+) rows=(\d+) max_radius=([\d.]+) "
    r"radius_sum=([\d.]+) largest_cell=(\d+)"
)
DECODE_RE = re.compile(r"scalar_decodes=(\d+) vector_decodes=(\d+)")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def run(config, binary, rows, dim, batch, cpus, text, selectivities):
    work = tempfile.mkdtemp(prefix="mvdb-sel-")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    body = "x" * text if text else ""
    result = {"rows": rows, "text": text, "queries": []}
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

            for categories in selectivities:
                # The filter column holds `categories` categories, so
                # one of them matches one row in `categories`. A column
                # with more than a thousand categories is not indexed by
                # the inverted index, so the narrow cases take the
                # unindexed path and the wide ones take the indexed
                # path; both are measured.
                cats = ["'c%d'" % (row_id % categories) for row_id in range(rows)]
                values = []
                for row_id in range(rows):
                    vec = ",".join(
                        str((row_id * (i + 1)) % 100) for i in range(dim)
                    )
                    if text:
                        values.append(
                            "(%d,%s,'%s','[%s]')"
                            % (row_id, cats[row_id], body, vec)
                        )
                    else:
                        values.append(
                            "(%d,%s,'[%s]')" % (row_id, cats[row_id], vec)
                        )
                session.request("DELETE FROM t;")
                for first in range(0, rows, batch):
                    session.request(
                        "INSERT INTO t VALUES "
                        + ",".join(values[first:first + batch]) + ";", "exact"
                    )
                session.request("CREATE VECTOR INDEX idx_t ON t(v);")

                sql = (
                    "SELECT id FROM t WHERE cat = 'c3' "
                    "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
                )
                started = time.monotonic()
                response = session.request(sql, "ann")
                took = time.monotonic() - started
                result["queries"].append({
                    "categories": categories,
                    "seconds": round(took, 3),
                    "rows_returned": len(response.get("rows", []) or []),
                })
                print("  selectivity 1 in %-6d %8.3fs  rows %d"
                      % (categories, took, len(response.get("rows", []) or [])))
        finally:
            try:
                session.shutdown()
            except Exception:
                pass
            session.close()
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            log = handle.read()
        result["scans"] = [
            {"cells": int(m.group(1)), "rows_measured": int(m.group(2)),
             "max_radius": float(m.group(3)), "radius_sum": float(m.group(4)),
             "largest_cell": int(m.group(5))}
            for m in STATS_RE.finditer(log)
        ]
        result["decodes"] = [
            {"scalar": int(m.group(1)), "vector": int(m.group(2))}
            for m in DECODE_RE.finditer(log)
        ]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", action="append", required=True)
    parser.add_argument("--rows", type=int, default=400000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    report = {"args": vars(args), "runs": []}
    selectivities = [8, 64, 512, 4096]
    for binary in args.binary:
        print("%s (rows=%d text=%d)" % (os.path.basename(binary), args.rows, args.text))
        result = run(
            config, binary, args.rows, args.dim, args.batch, args.cpus,
            args.text, selectivities,
        )
        report["runs"].append(result)
        for scan, decode in zip(result["scans"], result["decodes"]):
            print(
                "    -> cells=%-3d rows_measured=%-8d scalar_decodes=%-7d "
                "vector_decodes=%d"
                % (
                    scan["cells"], scan["rows_measured"],
                    decode["scalar"], decode["vector"],
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
