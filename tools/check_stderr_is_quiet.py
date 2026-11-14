#!/usr/bin/env python3
"""Check that the standard error of the database is empty by default.

The database prints what it measures to the standard error. Those writes
are **off by default**, and the reason is a measurement: a synchronous
write to a pipe costs a system call, and it blocks when the reader does
not drain the pipe.

The tool therefore checks two things:

* with no environment variable, the standard error of a database that
  answers a query is **empty**;
* with `MINIVECDB_DIAG=1`, the diagnostics are **there**, so that the
  switch is not a switch that turns everything off.

The second check matters as much as the first: a diagnostic that cannot be
turned on is not a diagnostic.

Usage:
    tools/check_stderr_is_quiet.py --binary PATH
"""

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile


def drive(binary, data_dir, env, sql):
    """Start the database, run one query, and return its standard error."""
    full = dict(os.environ)
    full.update(env)
    full["MINIVECDB_DATA_DIR"] = data_dir
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=full, text=True, bufsize=1,
    )
    ready = proc.stdout.readline()
    if '"ready"' not in ready:
        raise RuntimeError("the database did not say ready: %r" % ready)
    proc.stdin.write(json.dumps({
        "id": 1, "op": "execute", "sql": sql, "search_mode": "ann",
    }) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database did not answer")
    answer = json.loads(line)
    proc.stdin.write(json.dumps({"id": 2, "op": "shutdown"}) + "\n")
    proc.stdin.flush()
    proc.stdout.readline()
    proc.stdin.close()
    proc.wait(timeout=60)
    return answer, proc.stderr.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-stderr-")
    failures = []
    try:
        data_dir = os.path.join(work, "data")
        setup = [
            "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));",
            "INSERT INTO t VALUES (1,'a','[0,0,0,0]'),(2,'a','[1,0,0,0]'),"
            "(3,'b','[0,1,0,0]'),(4,'b','[2,0,0,0]');",
            "CREATE VECTOR INDEX idx_t ON t(v);",
        ]
        for sql in setup:
            answer, _ = drive(args.binary, data_dir, {}, sql)
            if answer.get("ok") is not True:
                failures.append("the setup failed: %s -> %r" % (sql, answer))
        query = ("SELECT id, L2_DISTANCE(v, '[0,0,0,0]') AS d FROM t "
                 "WHERE cat = 'a' ORDER BY d LIMIT 2;")

        # The default: the standard error must be empty.
        answer, err = drive(args.binary, data_dir, {}, query)
        if answer.get("ok") is not True:
            failures.append("the query failed: %r" % answer)
        quiet = err.strip()
        print("the default: %d bytes on the standard error" % len(quiet))
        if quiet:
            failures.append("the standard error is not empty by default:\n%s"
                            % quiet[:800])

        # The switch: the diagnostics must be there.
        #
        # The scan of the index is only reached with a **finite** number of
        # visited cells: with no limit the statement is answered by the exact
        # path, which is the fix of this round, and the scan of the index
        # never runs.
        answer, err = drive(
            args.binary, data_dir,
            {"MINIVECDB_DIAG": "1", "MINIVECDB_ANN_NPROBE": "4"}, query)
        loud = err.strip()
        print("with MINIVECDB_DIAG=1: %d bytes on the standard error" % len(loud))
        if "ann scan" not in loud:
            failures.append("the diagnostics are not printed with "
                            "MINIVECDB_DIAG=1:\n%s" % loud[:800])

        # The switch must also be off when it says so.
        answer, err = drive(args.binary, data_dir, {"MINIVECDB_DIAG": "0"}, query)
        if err.strip():
            failures.append("MINIVECDB_DIAG=0 still prints:\n%s" % err.strip()[:800])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report = {
        "binary": args.binary,
        "failures": failures,
        "passed": not failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
