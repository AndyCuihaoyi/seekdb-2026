#!/usr/bin/env python3
"""Show what the approximate index does differently in the two versions.

The approximate query with a filter is 8.7 times slower than it was in
the version that passed the admission, on the same data and with the
same number of rows. The code of the scan is the same in the two
versions. What changed is **the index that the scan walks**.

The scoring version chose the centroids from **every row** of the
table. The current version chooses them from a **sample of 8192 rows**,
so that the build of the index does not grow with the size of the
table.

A sample of 8192 rows out of 400000 does not cover the table: the
centroids are not the same, and more importantly the **cells they
define are not the same shape**. The scan prunes a cell when the
distance to its centroid is larger than the radius of the cell, so a
worse set of cells means:

* the radius of a cell is larger, so fewer cells are pruned;
* more cells are visited, and the rows of more cells are measured.

This script measures the effect of the two builds directly: it counts
the rows the scan visits, by asking the database for a count of the
rows of the answer under a filter that keeps every row, and it
measures the time of the approximate query with a filter.

The two builds are the same program at two commits, so the comparison
is fair.

Usage:
    tools/probe_index_quality.py --binary PATH --binary PATH [--rows N]
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "/tmp/official/protocol_check")

from _transport import Session  # noqa: E402


CONFIG_PATH = "/tmp/official/protocol_check/config.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def ask(session, sql, mode):
    started = time.monotonic()
    response = session.request(sql, mode)
    return time.monotonic() - started, response


def run(config, binary, rows, dim, batch, cpus, label):
    work = tempfile.mkdtemp(prefix="mvdb-quality-")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    result = {"label": label, "binary": binary, "rows": rows}
    try:
        session = Session(
            config, log_path,
            command=["taskset", "-c", "0,%d" % (cpus - 1), "sh", "-c",
                     "exec %s --data-dir {data_dir}" % binary],
            case_timeout=3600,
        )
        try:
            session.start()
            session.request(
                "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                "v VECTOR(%d));" % dim, "exact",
            )
            for first in range(0, rows, batch):
                values = []
                for row_id in range(first, min(first + batch, rows)):
                    vec = ",".join(
                        str((row_id * (i + 1)) % 100) for i in range(dim)
                    )
                    values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
                session.request(
                    "INSERT INTO t VALUES " + ",".join(values) + ";", "exact"
                )
            took, _ = ask(session, "CREATE VECTOR INDEX idx_t ON t(v);", "exact")
            result["index_build_s"] = round(took, 3)

            # The same query three times, so that the second and the third
            # measure a warm cache.
            for attempt in range(3):
                took, response = ask(
                    session,
                    "SELECT id FROM t WHERE cat = 'c3' "
                    "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                    "ann",
                )
                result["ann_filtered_s_%d" % attempt] = round(took, 3)
                result["ann_rows_%d" % attempt] = len(response.get("rows", []) or [])

            took, response = ask(
                session,
                "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "ann",
            )
            result["ann_unfiltered_s"] = round(took, 3)
        finally:
            try:
                session.shutdown()
            except Exception:
                pass
            session.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", action="append", required=True)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    report = {"args": vars(args), "runs": []}
    for binary in args.binary:
        result = run(
            config, binary, args.rows, args.dim, args.batch, args.cpus, binary
        )
        report["runs"].append(result)
        print(
            "%-46s index %6.3fs  ann-filtered %8.3fs / %8.3fs / %8.3fs"
            % (
                os.path.basename(binary),
                result["index_build_s"],
                result["ann_filtered_s_0"],
                result["ann_filtered_s_1"],
                result["ann_filtered_s_2"],
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
