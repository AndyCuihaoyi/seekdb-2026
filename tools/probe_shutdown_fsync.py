#!/usr/bin/env python3
"""The cost of the shutdown, against the size of the data.

The judge charges the shutdown to the same budget as a statement
(`request_timeout`, sixty seconds), and the shutdown of the engine does
two different things:

* `wal.flush()` - with `--durability=os` the log is only *written*, not
  fsynced, so the cost of it is the cost of the write;
* `store.sync()` - the fsync of every table file, which is the cost that
  grows with the size of the data.

The point of this script is to measure the second one and to say how much
data would need to exceed the budget of the judge.

The session is the official one (`/tmp/official/protocol_check`), so the
numbers are the numbers the judge would measure, and the database runs
under the limits of the judge: two CPUs and a two gigabyte address space.

Usage:
    tools/probe_shutdown_fsync.py --binary PATH [--rows N[,N,...]]
                                 [--text T] [--dim D] [--batch N]
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


def build_command(binary, cpus):
    """The argv of the database, with the limits of the judge applied."""
    inner = (
        "ulimit -v %d 2>/dev/null || true; exec %s --data-dir {data_dir}"
        % (2 * 1024 * 1024, binary)
    )
    if cpus:
        return ["taskset", "-c", "0,%d" % (cpus - 1), "sh", "-c", inner]
    return ["sh", "-c", inner]


def dir_size(path):
    """The size of the data directory, in bytes."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def run_one(config, binary, rows, text, dim, batch, cpus, log_path):
    """Write the rows, then time the shutdown."""
    out = {"rows": rows, "text": text, "dim": dim}
    try:
        session = Session(
            config,
            log_path,
            command=build_command(binary, cpus),
            case_timeout=3600,
        )
        # The transport owns the data directory: the database is started
        # in it, and it is removed when the session is closed. Its size is
        # the size of the data on the disk.
        data_dir = session.temp.name
        session.start()
        session.request(
            "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
            "body VARCHAR(%d), v VECTOR(%d));" % (max(text, 1), dim)
        )
        body = "x" * text if text else ""
        started = time.monotonic()
        for first in range(0, rows, batch):
            values = []
            for row_id in range(first, min(first + batch, rows)):
                vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(dim))
                if text:
                    values.append("(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec))
                else:
                    values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
            answer = session.request("INSERT INTO t VALUES " + ",".join(values) + ";")
            if answer is not None and answer.get("ok") is not True:
                out["failure"] = "the insert was refused: %s" % str(answer)[:200]
                session.close()
                return out
        out["write_s"] = round(time.monotonic() - started, 3)
        out["data_dir_bytes"] = dir_size(data_dir)

        # The shutdown: `wal.flush()` and `store.sync()` are inside it.
        started = time.monotonic()
        session.shutdown()
        out["shutdown_s"] = round(time.monotonic() - started, 3)
        session.close()
        return out
    finally:
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", default="200000,400000,800000,1600000")
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    budget = config["request_timeout"]

    work = tempfile.mkdtemp(prefix="mvdb-fsync-log-")
    log_path = os.path.join(work, "stderr.log")
    report = {"args": vars(args), "budget_s": budget, "points": []}
    try:
        for rows in [int(x) for x in args.rows.split(",") if x.strip()]:
            point = run_one(
                config, args.binary, rows, args.text, args.dim, args.batch, args.cpus, log_path
            )
            point["over_budget"] = point.get("shutdown_s", 0) > budget
            report["points"].append(point)
            print(
                "rows=%-9d data=%8.1f MiB  write=%8.3fs  shutdown=%7.3fs  %s"
                % (
                    point["rows"],
                    point.get("data_dir_bytes", 0) / (1024.0 * 1024.0),
                    point.get("write_s", 0.0),
                    point.get("shutdown_s", 0.0),
                    "OVER BUDGET" if point["over_budget"] else "within budget",
                )
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
