#!/usr/bin/env python3
"""Kill the database at a random moment of a write and check the restart.

The competition simulates a crash by forcing the process to terminate. A
`SIGKILL` can land in the middle of a `write_all`, so a directory entry
can be left half written: the offset it names is then a mixture of the
old value and the new one.

The startup of the database reads the directory of every table, and the
rebuild of the inverted indexes reads the row images it names. A read
that trusts a damaged offset panics, and a process that panics before the
handshake is what the platform reports as `database exited before
response`.

This script kills the database while it writes, restarts it, and checks
that the restart answers. Every kill is at a random moment, so the write
is cut at a random point.

Usage:
    tools/fuzz_kill_write.py [--binary PATH] [--rounds N] [--seed N]
"""

import argparse
import json
import os
import random
import select
import shutil
import subprocess
import sys
import tempfile
import time


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
        raise RuntimeError(
            "the database closed its output before the handshake: %s"
            % proc.stderr.read()[-1500:]
        )
    return proc


def run(proc, sql, timeout=10.0):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
    try:
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, ValueError):
        raise RuntimeError("the database closed its input")
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError("the database did not answer %r" % sql)
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(
            "the database exited while running %r: %s"
            % (sql, proc.stderr.read()[-1500:])
        )
    return json.loads(line)


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
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    failures = 0
    for round_no in range(args.rounds):
        rng = random.Random(args.seed + round_no)
        work = tempfile.mkdtemp(prefix="mvdb-killwrite-")
        data_dir = os.path.join(work, "data")
        try:
            # The first run creates the table and fills it.
            proc = start(args.binary, data_dir)
            run(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));")
            for i in range(20):
                run(proc, "INSERT INTO t VALUES (%d, 'c%d', '[%d,0,0,0]');" % (i, i % 3, i))
            proc.stdin.write(json.dumps({"id": 999, "op": "shutdown"}) + "\n")
            proc.stdin.flush()
            proc.stdout.readline()
            proc.wait(timeout=10)

            # The second run is killed while it writes, at a random moment.
            proc = start(args.binary, data_dir)
            rows = rng.randint(20, 400)
            try:
                for i in range(rows):
                    proc.stdin.write(
                        json.dumps(
                            {
                                "id": i + 1,
                                "op": "execute",
                                "sql": "INSERT INTO t VALUES (%d, 'c%d', '[%d,0,0,0]');"
                                % (1000 + i, i % 3, i),
                            }
                        )
                        + "\n"
                    )
                proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                pass
            time.sleep(rng.uniform(0.0, 0.08))
            proc.kill()
            proc.wait()

            # The third run has to start and answer.
            proc = start(args.binary, data_dir)
            try:
                for sql in (
                    "SELECT id FROM t LIMIT 3;",
                    "SELECT id FROM t WHERE cat = 'c1' ORDER BY id LIMIT 3;",
                ):
                    answer = run(proc, sql)
                    if answer.get("ok") is not True:
                        raise RuntimeError(
                            "the database answered %r with a failure: %r" % (sql, answer)
                        )
            except RuntimeError as exc:
                failures += 1
                print(
                    "FAIL  round %d (seed %d): %s" % (round_no, args.seed + round_no, exc),
                    file=sys.stderr,
                )
                print("      the data directory is at %s" % data_dir, file=sys.stderr)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            if args.verbose and failures == 0:
                print("ok    round %-4d killed during %d writes" % (round_no, rows))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\n%d round(s) failed" % failures, file=sys.stderr)
        return 1
    print("\nall %d rounds passed" % args.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
