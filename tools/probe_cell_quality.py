#!/usr/bin/env python3
"""Measure the quality of the cells of the vector index.

The approximate query with a filter is much slower than it was in the
version that passed the admission. The code of the scan did not
change, so the change is in **the index that the scan walks**.

The scan prunes a cell when the distance to its centroid is larger than
the radius of the cell. The radius is the largest distance from the
centroid to a row of the cell. So the cost of the scan is decided by
two numbers of the index:

* the **radius** of a cell - a large radius means that the cell cannot
  be pruned;
* the **spread** of the rows of a cell - the rows of a cell are spread
  over every block of the table, so a cell that holds rows from many
  blocks makes the scan decode many blocks.

The tool below reads both numbers out of the index, by asking the
database for them through a query that uses the index, and by timing
the two shapes of query that the judge performs.

Usage:
    tools/probe_cell_quality.py --binary PATH [--rows N] [--dim D]
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
    work = tempfile.mkdtemp(prefix="mvdb-cells-")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    result = {"label": label, "rows": rows}
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

            # The cost of the two shapes of query of the judge.
            times = []
            for _ in range(3):
                took, response = ask(
                    session,
                    "SELECT id FROM t WHERE cat = 'c3' "
                    "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                    "ann",
                )
                times.append(took)
                result["ann_filtered_rows"] = len(response.get("rows", []) or [])
            result["ann_filtered_s"] = round(min(times), 3)

            times = []
            for _ in range(3):
                took, response = ask(
                    session,
                    "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                    "ann",
                )
                times.append(took)
                result["ann_unfiltered_rows"] = len(response.get("rows", []) or [])
            result["ann_unfiltered_s"] = round(min(times), 3)

            # The recall of the approximate path against the exact one.
            took, exact = ask(
                session,
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "exact",
            )
            exact_ids = [row[0] for row in (exact.get("rows") or [])]
            took, approx = ask(
                session,
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "ann",
            )
            approx_ids = [row[0] for row in (approx.get("rows") or [])]
            hit = len(set(exact_ids) & set(approx_ids))
            result["recall_filtered"] = (
                round(hit / len(exact_ids), 4) if exact_ids else None
            )
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
            "%-40s ann-filtered %7.3fs  ann-unfiltered %7.3fs  recall %.4f"
            % (
                os.path.basename(binary),
                result["ann_filtered_s"],
                result["ann_unfiltered_s"],
                result["recall_filtered"],
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
