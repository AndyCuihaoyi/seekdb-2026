#!/usr/bin/env python3
"""Show why the approximate query with a filter is so much slower.

The sweep of the classification run showed that the approximate query
with a filter costs far more than the exact one, and that the cost
grows with the size of the table:

    rows    exact + filter    ann + filter
    100000        0.08 s            1.78 s
    400000        0.34 s            7.70 s
   1600000        1.41 s           34.51 s

This script checks the hypothesis that explains it.

The hypothesis: **the approximate path visits the rows cell by cell,
and the rows of one cell are spread over every block of the table.**
The filter needs the scalar columns of a row, and the scalar columns
of a block are decoded as one unit. So a block is decoded once for
every cell that holds one of its rows - `nlist` times over - while the
exact path walks the table block by block and decodes each block once.

The cost of the approximate path is therefore about `nlist` times the
cost of the exact one, and it only shows when the block is **large**,
which means a table with a long text column. A table without one has
small blocks, and the cost of decoding them is not visible.

The measurements are:

* the same query, with and without a long text column;
* the same query, with a small `nprobe` and with the default one.

If the hypothesis holds, the approximate query with a text column is
slow, the one without is fast, and a small `nprobe` makes the slow one
fast again - because a small `nprobe` visits few cells, and therefore
touches few blocks.

Usage:
    tools/probe_ann_filter_cost.py --binary PATH [--rows N] [--text T]
                                    [--dim D] [--cpus N]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/tmp/official/protocol_check")

from _transport import Session  # noqa: E402


CONFIG_PATH = "/tmp/official/protocol_check/config.json"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def make_session(config, binary, cpus, log_path, nprobe=None):
    inner = "exec %s --data-dir {data_dir}" % binary
    argv = ["sh", "-c", inner]
    if cpus:
        argv = ["taskset", "-c", "0,%d" % (cpus - 1)] + argv
    if nprobe:
        argv = ["sh", "-c", inner.replace(
            "--data-dir {data_dir}",
            "--data-dir {data_dir} --ann-nprobe %d" % nprobe,
        )]
        argv = ["taskset", "-c", "0,%d" % (cpus - 1)] + argv
    return Session(
        config, log_path, command=argv, case_timeout=3600,
    )


def ask(session, sql, mode):
    started = time.monotonic()
    response = session.request(sql, mode)
    return time.monotonic() - started, response


def build(session, rows, text, dim, batch):
    """Fill a table with a long text column and build the index."""
    body = "x" * text if text else ""
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
            vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(dim))
            if text:
                values.append(
                    "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
                )
            else:
                values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
        session.request("INSERT INTO t VALUES " + ",".join(values) + ";", "exact")
    took, _ = ask(session, "CREATE VECTOR INDEX idx_t ON t(v);", "exact")
    return took


def measure(config, binary, rows, text, dim, batch, cpus, label, nprobe=None):
    work = tempfile.mkdtemp(prefix="mvdb-ann-")
    log_path = os.path.join(work, "stderr.log")
    probe = ",".join(str(i % 100) for i in range(dim))
    result = {"label": label, "rows": rows, "text": text, "nprobe": nprobe}
    try:
        session = make_session(config, binary, cpus, log_path, nprobe)
        try:
            session.start()
            result["index_build_s"] = round(build(session, rows, text, dim, batch), 3)

            took, response = ask(
                session,
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "exact",
            )
            result["exact_filtered_s"] = round(took, 3)
            result["exact_rows"] = len(response.get("rows", []) or [])

            took, response = ask(
                session,
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
                "ann",
            )
            result["ann_filtered_s"] = round(took, 3)
            result["ann_rows"] = len(response.get("rows", []) or [])

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
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    report = {"args": vars(args), "runs": []}

    # The four runs of the hypothesis: with and without a long text
    # column, with the default number of probes and with a small one.
    for text in (1000, 0):
        for nprobe in (None, 4):
            result = measure(
                config, args.binary, args.rows, text, args.dim,
                args.batch, args.cpus, "text=%d nprobe=%s" % (text, nprobe),
                nprobe,
            )
            report["runs"].append(result)
            print(
                "%-28s exact %8.3fs   ann-filtered %8.3fs   ann-unfiltered %8.3fs"
                % (
                    result["label"],
                    result["exact_filtered_s"],
                    result["ann_filtered_s"],
                    result["ann_unfiltered_s"],
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
