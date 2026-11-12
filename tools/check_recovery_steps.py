#!/usr/bin/env python3
"""Attribute the memory of the recovery to the steps of the recovery.

The recovery of a large table costs more memory than the budget allows, and
the recovery has several steps that could be the cause. This script starts
the database on a data directory that is already written and reports the
memory of the process at several moments:

* **before** the process is started;
* **after the handshake** - the recovery, the rebuild of the primary key
  index, the rebuild of the inverted indexes and the rebuild of the vector
  index are all done by then;
* **after a query** - a query that walks the whole table.

The numbers come from the process itself, read out of `/proc/<pid>/status`,
so they are the memory of the database and not of the measurement.

Usage:
    tools/check_recovery_steps.py [--binary PATH] [--rows N] [--dim N]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


def rss_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def hwm_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def start(binary, data_dir):
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir
    env["MINIVECDB_MODE"] = "exact"
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError("the database closed its output: %s" % proc.stderr.read()[-1500:])
    return proc


def ask(proc, sql):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database exited: %s" % proc.stderr.read()[-1500:])
    return json.loads(line)


def close(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=60)
    except Exception:
        proc.kill()


def write_data(binary, data_dir, rows, dim):
    proc = start(binary, data_dir)
    try:
        ask(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim)
        batch = 500
        for first in range(0, rows, batch):
            values = []
            for row_id in range(first, min(first + batch, rows)):
                values.append(
                    "(%d,'c%d','[%s]')"
                    % (
                        row_id,
                        row_id % 8,
                        ",".join(str((row_id * (i + 1)) % 100) for i in range(dim)),
                    )
                )
            ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
    finally:
        close(proc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "minivectordb", "target", "release", "minivectordb",
        ),
    )
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--dim", type=int, default=128)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-steps-")
    data_dir = os.path.join(work, "data")
    try:
        write_data(args.binary, data_dir, args.rows, args.dim)

        proc = start(args.binary, data_dir)
        try:
            at_handshake = rss_kb(proc.pid)
            peak_at_handshake = hwm_kb(proc.pid)
            ask(proc, "SELECT id FROM t LIMIT 1;")
            time.sleep(0.3)
            after_small_query = rss_kb(proc.pid)
            peak_after_small = hwm_kb(proc.pid)
            ask(proc, "SELECT id, L2_DISTANCE(v, '[1,2,3,4]') AS d FROM t ORDER BY d LIMIT 10;")
            time.sleep(0.3)
            after_scan = rss_kb(proc.pid)
            peak_after_scan = hwm_kb(proc.pid)
        finally:
            close(proc)

        print(
            json.dumps(
                {
                    "rows": args.rows,
                    "rss_after_handshake_mib": at_handshake // 1024,
                    "peak_after_handshake_mib": peak_at_handshake // 1024,
                    "rss_after_limit1_mib": after_small_query // 1024,
                    "peak_after_limit1_mib": peak_after_small // 1024,
                    "rss_after_full_scan_mib": after_scan // 1024,
                    "peak_after_full_scan_mib": peak_after_scan // 1024,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
