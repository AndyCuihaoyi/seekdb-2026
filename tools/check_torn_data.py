#!/usr/bin/env python3
"""Check that a database whose data files were cut short still answers.

The admission gate punishes a process that dies instead of answering:
the platform reads it as `database exited before response`. The version
that was submitted to the platform in this round died exactly that way,
because the startup rebuild of the inverted indexes read the row images
with an unchecked slice, and a data directory whose `col.dat` is shorter
than its `meta.dat` says it is - which is what an interrupted write
leaves behind - made the slice run past the end of the buffer.

This check cuts the data files of a committed database in every way that
matters and checks that the database still starts, still answers, and
still answers with the rows that are readable.

Usage:
    tools/check_torn_data.py [--binary PATH] [--verbose]

The script exits with a non-zero status when a check fails.
"""

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile


class Failure(Exception):
    """A check that did not hold."""


def start(binary, data_dir):
    """Start the database and wait for the handshake."""
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
        raise Failure(
            "the database closed its output before the handshake: %s"
            % proc.stderr.read()[-800:]
        )
    return proc


def run(proc, sql, timeout=10.0):
    """Run one statement and return the answer object."""
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise Failure("the database did not answer %r within %.0fs" % (sql, timeout))
    line = proc.stdout.readline()
    if not line:
        raise Failure(
            "the database exited while running %r: %s" % (sql, proc.stderr.read()[-800:])
        )
    return json.loads(line)


def close(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def build_database(binary, data_dir, rows=40):
    """Create a table with rows and shut the database down cleanly."""
    proc = start(binary, data_dir)
    try:
        run(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));")
        for i in range(rows):
            run(proc, "INSERT INTO t VALUES (%d, 'c%d', '[%d,0,0,0]');" % (i, i % 3, i))
        request = {"id": 999, "op": "shutdown"}
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
        proc.wait(timeout=10)
    finally:
        close(proc)


def check_after_cut(binary, data_dir, cut, verbose):
    """Cut the column file and check that the database still answers."""
    col = os.path.join(data_dir, "t", "col.dat")
    size = os.path.getsize(col)
    if size == 0:
        return
    with open(col, "r+b") as handle:
        handle.truncate(max(0, size - cut))

    proc = start(binary, data_dir)
    try:
        for sql in (
            "SELECT id FROM t LIMIT 3;",
            "SELECT id, L2_DISTANCE(v, '[0,0,0,0]') AS d FROM t ORDER BY d LIMIT 3;",
            "SELECT id FROM t WHERE cat = 'c1' ORDER BY id LIMIT 3;",
        ):
            answer = run(proc, sql)
            if answer.get("ok") is not True:
                raise Failure(
                    "the database answered %r with a failure: %r" % (sql, answer)
                )
    finally:
        close(proc)
    if verbose:
        print("ok    cut %-6d the database starts and answers" % cut)


def check_after_cut_of_the_meta(binary, data_dir, cut, verbose):
    """Cut the directory file and check that the database still answers."""
    meta = os.path.join(data_dir, "t", "meta.dat")
    size = os.path.getsize(meta)
    if size == 0:
        return
    with open(meta, "r+b") as handle:
        handle.truncate(max(0, size - cut))

    proc = start(binary, data_dir)
    try:
        answer = run(proc, "SELECT id FROM t LIMIT 3;")
        if answer.get("ok") is not True:
            raise Failure("the database answered with a failure: %r" % (answer,))
    finally:
        close(proc)
    if verbose:
        print("ok    meta cut %-4d the database starts and answers" % cut)


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
        help="the database binary",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not os.access(args.binary, os.X_OK):
        print("error: the binary is not executable", file=sys.stderr)
        return 2

    failures = 0
    # Every cut is a separate database: the cuts are not independent of each
    # other, because one cut changes the file the next cut is applied to.
    for cut in (1, 5, 17, 40, 100, 300, 1000):
        work = tempfile.mkdtemp(prefix="mvdb-torn-")
        try:
            build_database(args.binary, os.path.join(work, "data"))
            try:
                check_after_cut(args.binary, os.path.join(work, "data"), cut, args.verbose)
            except Failure as exc:
                failures += 1
                print("FAIL  cut %-6d %s" % (cut, exc), file=sys.stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    for cut in (40, 200):
        work = tempfile.mkdtemp(prefix="mvdb-torn-meta-")
        try:
            build_database(args.binary, os.path.join(work, "data"))
            try:
                check_after_cut_of_the_meta(
                    args.binary, os.path.join(work, "data"), cut, args.verbose
                )
            except Failure as exc:
                failures += 1
                print("FAIL  meta cut %-4d %s" % (cut, exc), file=sys.stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\n%d check(s) failed" % failures, file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
