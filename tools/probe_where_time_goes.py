#!/usr/bin/env python3
"""Find the statement the judge would wait for, and the build that waits.

The platform reported `JSON response timeout` for the version of the round
before the last one. The judge raises that message in exactly one place,
`Session.read()`, and it means one of three things:

* the handshake of the start-up did not arrive within `startup_timeout`,
* a single statement did not answer within `request_timeout`,
* the `shutdown` did not answer within `request_timeout`.

The message does not say which one, and it does not say which statement.
This script measures each of them **separately**, for each build it is
given, so that the answer is a name and a number rather than a guess.

Two details of the measurement matter:

* The time of a batch of inserts is reported for the **first** and the
  **last** batch separately. A cost that grows with the number of rows is
  invisible in the mean of a run whose early batches are cheap, and fatal
  in the last one.
* The handshake is measured on a directory that **already holds the
  table**, because that is the one the judge charges to `startup_timeout`:
  the recovery and the rebuild of the indexes happen before the handshake.

Usage:
    tools/probe_where_time_goes.py --binary PATH [--binary PATH ...]
                                   [--rows N] [--dim N] [--batch N]
                                   [--limit-mb N]
"""

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time


def start(binary, data_dir, limit_bytes):
    """Start the database and return it with the time of the handshake."""
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir

    def preexec():
        if limit_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
        preexec_fn=preexec,
    )
    started = time.monotonic()
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError(
            "the database exited before the handshake: %s" % proc.stderr.read()[-2000:]
        )
    return proc, time.monotonic() - started


def ask(proc, sql, mode="exact"):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(
            "the database exited before the response: %s" % proc.stderr.read()[-2000:]
        )
    return json.loads(line)


def shutdown(proc):
    """Send the shutdown request and time it, the way the judge does."""
    request = {"id": 999, "op": "shutdown"}
    started = time.monotonic()
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    elapsed = time.monotonic() - started
    ok = bool(line)
    try:
        proc.stdin.close()
        proc.wait(timeout=180)
    except Exception:
        proc.kill()
    return elapsed, ok


def close(proc):
    try:
        proc.stdin.close()
        proc.wait(timeout=180)
    except Exception:
        proc.kill()


def run(binary, rows, dim, batch, limit_bytes):
    """Time every phase of one build."""
    work = tempfile.mkdtemp(prefix="mvdb-probe-")
    data_dir = os.path.join(work, "data")
    m = {"binary": binary, "rows": rows, "dim": dim}
    try:
        proc, handshake = start(binary, data_dir, limit_bytes)
        try:
            m["handshake_fresh_s"] = round(handshake, 3)
            ask(
                proc,
                "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                "v VECTOR(%d));" % dim,
            )

            # The inserts, in batches. The first and the last batch are
            # timed separately.
            started = time.monotonic()
            first_s = None
            last_s = None
            for first in range(0, rows, batch):
                values = []
                for row_id in range(first, min(first + batch, rows)):
                    vec = ",".join(
                        str((row_id * (i + 1)) % 100) for i in range(dim)
                    )
                    values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
                one = time.monotonic()
                answer = ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
                took = time.monotonic() - one
                if first_s is None:
                    first_s = took
                last_s = took
                if answer.get("ok") is not True:
                    m["error"] = "the insert was refused: %s" % str(answer)[:200]
                    return m
            m["write_total_s"] = round(time.monotonic() - started, 3)
            m["write_first_batch_s"] = round(first_s, 4)
            m["write_last_batch_s"] = round(last_s, 4)
            # The ratio of the cost of the last batch to the cost of the
            # first one. A value near one means the cost per row does not
            # grow with the size of the table.
            m["write_growth"] = round(last_s / max(first_s, 1e-9), 2)

            started = time.monotonic()
            answer = ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            m["index_build_s"] = round(time.monotonic() - started, 3)
            if answer.get("ok") is not True:
                m["error"] = "the index was not created: %s" % str(answer)[:200]
                return m

            probe = ",".join(str(i % 100) for i in range(dim))
            started = time.monotonic()
            ask(
                proc,
                "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
            )
            m["query_unfiltered_s"] = round(time.monotonic() - started, 3)

            started = time.monotonic()
            ask(
                proc,
                "SELECT id FROM t WHERE cat = 'c3' "
                "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
            )
            m["query_filtered_s"] = round(time.monotonic() - started, 3)

            started = time.monotonic()
            ask(proc, "SELECT id FROM t WHERE id = %d;" % (rows // 2))
            m["query_pk_s"] = round(time.monotonic() - started, 3)

            took, ok = shutdown(proc)
            m["shutdown_s"] = round(took, 3)
            m["shutdown_answered"] = ok
        except RuntimeError as e:
            m["error"] = str(e)
            close(proc)

        # The handshake on a directory that already holds the data: the
        # recovery and the rebuild of the indexes are charged to it.
        try:
            proc, handshake = start(binary, data_dir, limit_bytes)
            try:
                m["handshake_after_restart_s"] = round(handshake, 3)
            finally:
                close(proc)
        except RuntimeError as e:
            m["restart_error"] = str(e)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return m


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", action="append", required=True)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--limit-mb", type=int, default=2048)
    args = parser.parse_args()

    limit_bytes = args.limit_mb * 1024 * 1024 if args.limit_mb else None
    report = []
    for binary in args.binary:
        print("=== %s ===" % binary, flush=True)
        m = run(binary, args.rows, args.dim, args.batch, limit_bytes)
        report.append(m)
        for key in (
            "handshake_fresh_s",
            "write_total_s",
            "write_first_batch_s",
            "write_last_batch_s",
            "write_growth",
            "index_build_s",
            "query_unfiltered_s",
            "query_filtered_s",
            "query_pk_s",
            "shutdown_s",
            "shutdown_answered",
            "handshake_after_restart_s",
            "error",
            "restart_error",
        ):
            if key in m:
                print("  %-26s %s" % (key, m[key]), flush=True)

    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
