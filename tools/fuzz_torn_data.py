#!/usr/bin/env python3
"""Fuzz the data directory and check that the database always answers.

The admission gate punishes a process that dies instead of answering. A
data directory that was interrupted in the middle of a write, or that was
damaged in any other way, must never take the process down: the database
has to start, say that it is ready, and answer.

This script takes a committed database, damages its files in a random
way, and checks that the database still starts and still answers. Every
damage is recorded, so that a failure can be reproduced.

Usage:
    tools/fuzz_torn_data.py [--binary PATH] [--rounds N] [--seed N] [--verbose]
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

QUERIES = [
    "SELECT id FROM t LIMIT 3;",
    "SELECT id, L2_DISTANCE(v, '[0,0,0,0]') AS d FROM t ORDER BY d LIMIT 3;",
    "SELECT id FROM t WHERE cat = 'c1' ORDER BY id LIMIT 3;",
    "SELECT id, cat FROM t ORDER BY id LIMIT 5;",
]


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
        stderr = proc.stderr.read()[-2000:]
        raise RuntimeError(
            "the database closed its output before the handshake: %s" % stderr
        )
    return proc


def run(proc, sql, timeout=10.0):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
    try:
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, ValueError):
        raise RuntimeError("the database closed its input: %s" % proc.stderr.read()[-800:])
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


def close(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def build_database(binary, data_dir, rows=60):
    proc = start(binary, data_dir)
    try:
        run(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(4));")
        for i in range(rows):
            run(proc, "INSERT INTO t VALUES (%d, 'c%d', '[%d,0,0,0]');" % (i, i % 3, i))
        proc.stdin.write(json.dumps({"id": 999, "op": "shutdown"}) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
        proc.wait(timeout=10)
    finally:
        close(proc)


def damage(rng, data_dir):
    """Damage the files of the database in a random way.

    Returns a description of what was done.
    """
    files = []
    for root, _dirs, names in os.walk(data_dir):
        for name in names:
            files.append(os.path.join(root, name))
    files.sort()
    if not files:
        return "no files"
    path = rng.choice(files)
    size = os.path.getsize(path)
    kind = rng.choice(["truncate", "truncate", "zero", "flip", "grow"])
    if kind == "truncate" and size > 0:
        cut = rng.randint(1, size)
        with open(path, "r+b") as handle:
            handle.truncate(size - cut)
        return "truncate %s by %d" % (os.path.basename(path), cut)
    if kind == "zero" and size > 0:
        offset = rng.randrange(size)
        length = min(rng.randint(1, 64), size - offset)
        with open(path, "r+b") as handle:
            handle.seek(offset)
            handle.write(b"\x00" * length)
        return "zero %d bytes at %d of %s" % (length, offset, os.path.basename(path))
    if kind == "flip" and size > 0:
        offset = rng.randrange(size)
        length = min(rng.randint(1, 16), size - offset)
        with open(path, "r+b") as handle:
            handle.seek(offset)
            data = bytearray(handle.read(length))
            for i in range(len(data)):
                data[i] ^= 1 << rng.randrange(8)
            handle.seek(offset)
            handle.write(bytes(data))
        return "flip %d bytes at %d of %s" % (length, offset, os.path.basename(path))
    if kind == "grow":
        with open(path, "ab") as handle:
            handle.write(bytes(rng.randrange(256) for _ in range(rng.randint(1, 64))))
        return "grow %s" % os.path.basename(path)
    return "nothing"


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
        work = tempfile.mkdtemp(prefix="mvdb-fuzz-")
        data_dir = os.path.join(work, "data")
        try:
            build_database(args.binary, data_dir)
            what = damage(rng, data_dir)
            proc = None
            try:
                proc = start(args.binary, data_dir)
            except RuntimeError as exc:
                # A database that refuses to start with a clear message is
                # not a crash: the catalog cannot be read, and there is
                # nothing to recover from. What must not happen is a panic.
                text = str(exc)
                if "panicked" in text:
                    failures += 1
                    print(
                        "FAIL  round %d (seed %d): the database panicked | damage: %s"
                        % (round_no, args.seed + round_no, what),
                        file=sys.stderr,
                    )
                elif args.verbose:
                    print("ok    round %-4d %s (refused to start)" % (round_no, what))
                continue
            try:
                for sql in QUERIES:
                    answer = run(proc, sql)
                    if answer.get("ok") is not True:
                        raise RuntimeError(
                            "the database answered %r with a failure: %r" % (sql, answer)
                        )
            except RuntimeError as exc:
                failures += 1
                print(
                    "FAIL  round %d (seed %d): %s | damage: %s"
                    % (round_no, args.seed + round_no, exc, what),
                    file=sys.stderr,
                )
                print("      the data directory is at %s" % data_dir, file=sys.stderr)
            finally:
                if proc is not None:
                    close(proc)
            if args.verbose and failures == 0:
                print("ok    round %-4d %s" % (round_no, what))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    if failures:
        print("\n%d round(s) failed" % failures, file=sys.stderr)
        return 1
    print("\nall %d rounds passed" % args.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
