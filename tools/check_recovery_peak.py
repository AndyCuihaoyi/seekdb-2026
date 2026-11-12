#!/usr/bin/env python3
"""Measure the memory of the recovery alone.

The recovery is what happens between the start of the process and the
handshake. It is the part of the start that the statement charges against
the resource limits, and it is the part that has to work on a data set
that is larger than the memory budget.

The measurement reads the high water mark of the process right after the
handshake, so it covers the recovery and nothing else: no query is sent.

Usage:
    tools/check_recovery_peak.py [--rows N,N,N] [--dim N]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time


def hwm_kb(pid):
    try:
        with open("/proc/%d/status" % pid) as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def ask(proc, sql):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database exited")
    return json.loads(line)


def start(binary, data_dir):
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError("the database closed its output: %s" % proc.stderr.read()[-2000:])
    return proc


def close(proc):
    try:
        proc.stdin.close()
        proc.wait(timeout=60)
    except Exception:
        proc.kill()


def write_data(binary, data_dir, rows, dim, index):
    proc = start(binary, data_dir)
    try:
        ask(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim)
        for first in range(0, rows, 500):
            values = []
            for row_id in range(first, min(first + 500, rows)):
                values.append(
                    "(%d,'c%d','[%s]')"
                    % (
                        row_id,
                        row_id % 8,
                        ",".join(str((row_id * (i + 1)) % 100) for i in range(dim)),
                    )
                )
            ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
        if index:
            ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
    finally:
        close(proc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "minivectordb",
            "target",
            "release",
            "minivectordb",
        ),
    )
    parser.add_argument("--rows", default="50000,200000,800000")
    parser.add_argument("--dim", type=int, default=128)
    args = parser.parse_args()

    print("%9s %14s %14s %12s" % ("rows", "recovery", "recovery+index", "data dir"))
    for rows in [int(r) for r in args.rows.split(",")]:
        work = tempfile.mkdtemp(prefix="mvdb-recovery-")
        data_dir = os.path.join(work, "data")
        try:
            write_data(args.binary, data_dir, rows, args.dim, False)
            proc = start(args.binary, data_dir)
            plain = hwm_kb(proc.pid)
            close(proc)

            proc = start(args.binary, data_dir)
            try:
                ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            finally:
                close(proc)

            proc = start(args.binary, data_dir)
            with_index = hwm_kb(proc.pid)
            close(proc)

            total = 0
            for root, _dirs, files in os.walk(data_dir):
                for name in files:
                    total += os.path.getsize(os.path.join(root, name))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(
            "%9d %11d MiB %11d MiB %9d MiB"
            % (rows, plain // 1024, with_index // 1024, total // (1024 * 1024))
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
