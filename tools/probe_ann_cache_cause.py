#!/usr/bin/env python3
"""Separate the two causes of the slow approximate query.

The approximate query with a filter is 8.7 times slower than it was in
the version that passed the admission, and the cost grows with the size
of the table. Two changes were made between the two versions, and both
of them change the cost of the approximate path:

* the block of the vector file holds 1024 rows instead of 4096, so the
  table has four times as many blocks;
* the three block caches share one budget instead of each having the
  whole of it, so the cache holds a third of what it used to.

The approximate path visits the rows **cell by cell**, and the rows of
one cell are spread over every block of the table. The filter needs the
scalar columns of a row, and the scalar columns of a block are decoded
as one unit. So a block is decoded once for every cell that holds one
of its rows.

If the cost is dominated by the number of blocks, a larger block makes
the query faster. If it is dominated by the size of the cache, a larger
budget makes it faster. This script measures both, by starting the
database with a different block size and a different budget.

Usage:
    tools/probe_ann_cache_cause.py --binary PATH [--rows N] [--text T]
                                    [--dim D] [--cpus N]
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


def make_session(config, binary, cpus, log_path, block_rows=None, pool_mb=None):
    """Start the database with a block size and a cache budget."""
    flags = "--data-dir {data_dir}"
    if block_rows:
        flags += " --vector-block-rows %d" % block_rows
    if pool_mb:
        flags += " --buffer-pool-mb %d" % pool_mb
    argv = ["sh", "-c", "exec %s %s" % (binary, flags)]
    if cpus:
        argv = ["taskset", "-c", "0,%d" % (cpus - 1)] + argv
    return Session(config, log_path, command=argv, case_timeout=3600)


def ask(session, sql, mode):
    started = time.monotonic()
    response = session.request(sql, mode)
    return time.monotonic() - started, response


def one_run(config, binary, rows, text, dim, batch, cpus, block_rows, pool_mb, label):
    work = tempfile.mkdtemp(prefix="mvdb-cause-")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    body = "x" * text if text else ""
    result = {"label": label, "block_rows": block_rows, "pool_mb": pool_mb,
              "rows": rows, "text": text}
    try:
        session = make_session(
            config, binary, cpus, log_path, block_rows, pool_mb
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
            took, _ = ask(session, "CREATE VECTOR INDEX idx_t ON t(v);", "exact")
            result["index_build_s"] = round(took, 3)

            took, response = ask(
                session,
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "exact",
            )
            result["exact_filtered_s"] = round(took, 3)
            result["rows_returned"] = len(response.get("rows", []) or [])

            took, response = ask(
                session,
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "ann",
            )
            result["ann_filtered_s"] = round(took, 3)

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
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=400000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    report = {"args": vars(args), "runs": []}

    # The combinations that separate the two causes.
    combos = [
        (1024, 64, "current: block 1024, pool 64"),
        (4096, 64, "block 4096, pool 64"),
        (1024, 192, "block 1024, pool 192"),
        (4096, 192, "block 4096, pool 192"),
    ]
    for block_rows, pool_mb, label in combos:
        result = one_run(
            config, args.binary, args.rows, args.text, args.dim,
            args.batch, args.cpus, block_rows, pool_mb, label,
        )
        report["runs"].append(result)
        print(
            "%-32s ann-filtered %8.3fs  ann-unfiltered %8.3fs  rows %d"
            % (
                label,
                result["ann_filtered_s"],
                result["ann_unfiltered_s"],
                result["rows_returned"],
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
