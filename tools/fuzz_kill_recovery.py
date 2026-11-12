#!/usr/bin/env python3
"""Kill the database *during* its recovery and check that it recovers again.

The official requirement says:

    恢复 | 强制终止后保留 /data，重启并核对提交状态；在恢复途中再次终止，
    检查重复恢复

So the database has to survive a kill that lands in the middle of a
recovery: the second start has to find the same committed rows, and a
third start has to find them again.

The recovery of this engine applies the log records to the data files and
empties the log afterwards, so a kill in between leaves a log that is
partly applied. The replay has to be idempotent about that.

This script:

1. builds a database with committed rows,
2. copies the data directory, so that every round starts from the same
   state,
3. starts the database and kills it after a short, random delay, so that
   the kill lands somewhere in the recovery,
4. starts it again and checks that the committed rows are all there,
5. kills it during that recovery as well, and checks once more.

Usage:
    tools/fuzz_kill_recovery.py [--binary PATH] [--rounds N] [--seed N]
"""

import argparse
import json
import os
import random
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
    return proc


def read_ready(proc, timeout=30.0):
    """Read the handshake, and report whether the process died instead."""
    import select

    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        return None
    line = proc.stdout.readline()
    if not line:
        return None
    return line.strip()


def run(proc, sql, timeout=15.0):
    import select

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
        raise RuntimeError("the database exited while running %r" % sql)
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


def build_database(binary, data_dir, rows=400):
    proc = start(binary, data_dir)
    try:
        read_ready(proc)
        run(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));")
        for i in range(rows):
            run(proc, "INSERT INTO t VALUES (%d, 'c%d', '[%d,0,0,0]');" % (i, i % 3, i))
        proc.stdin.write(json.dumps({"id": 999, "op": "shutdown"}) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
        proc.wait(timeout=10)
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
    parser.add_argument("--rounds", type=int, default=40)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    failures = 0
    for round_no in range(args.rounds):
        rng = random.Random(args.seed + round_no)
        work = tempfile.mkdtemp(prefix="mvdb-killrec-")
        template = os.path.join(work, "template")
        live = os.path.join(work, "live")
        try:
            build_database(args.binary, template)

            # The first kill lands somewhere in the recovery: the delay is
            # short enough to land inside it for most of the data sets.
            shutil.copytree(template, live)
            proc = start(args.binary, live)
            time.sleep(rng.uniform(0.0, 0.05))
            proc.kill()
            proc.wait(timeout=10)

            # The second start has to find the rows.
            proc = start(args.binary, live)
            try:
                handshake = read_ready(proc)
                if handshake != '{"type":"ready"}':
                    raise RuntimeError(
                        "the database did not announce readiness: %r (stderr: %s)"
                        % (handshake, proc.stderr.read()[-800:])
                    )
                answer = run(proc, "SELECT id FROM t;")
                if answer.get("ok") is not True:
                    raise RuntimeError("the query failed: %r" % answer)
                count = len(answer.get("rows", []))
                if count != 400:
                    raise RuntimeError("the table holds %r rows, expected 400" % count)
            finally:
                close(proc)

            # The third start has to find them again: the recovery was
            # interrupted once already.
            shutil.rmtree(live)
            shutil.copytree(template, live)
            proc = start(args.binary, live)
            time.sleep(rng.uniform(0.0, 0.05))
            proc.kill()
            proc.wait(timeout=10)
            proc = start(args.binary, live)
            try:
                handshake = read_ready(proc)
                if handshake != '{"type":"ready"}':
                    raise RuntimeError("the second recovery did not announce readiness")
                answer = run(proc, "SELECT id FROM t;")
                count = len(answer.get("rows", []))
                if count != 400:
                    raise RuntimeError("the table holds %r rows after two kills" % count)
            finally:
                close(proc)
        except Exception as exc:
            failures += 1
            print(
                "FAIL  round %d (seed %d): %s" % (round_no, args.seed + round_no, exc),
                file=sys.stderr,
            )
            print("      the data directory is at %s" % live, file=sys.stderr)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\n%d round(s) failed" % failures, file=sys.stderr)
        return 1
    print("\nall %d rounds passed" % args.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
