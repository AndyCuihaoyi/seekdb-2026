#!/usr/bin/env python3
"""Run the database under the memory limit the judge uses.

The judge runs the container with `--memory=2g --memory-swap=2g`. A cgroup
of one's own cannot be created here without the permission to write the
cgroup tree, so the limit is applied to the address space of the child
instead: `RLIMIT_AS` is the same number of bytes, and an allocation that
does not fit fails in the same way - the process is killed before it can
answer.

The script drives the database over the standard input and output protocol
the way the judge does, writes a data set that is larger than the budget,
restarts the database on the same data directory and reports whether it
answered.

Usage:
    tools/check_memory_limit.py [--binary PATH] [--rows N] [--dim N]
                                [--text N] [--limit-mb N]
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


def limited():
    """The limit of the child, in bytes."""
    limit = int(os.environ.get("MVDB_LIMIT_BYTES", "0"))
    if not limit:
        return None
    return limit


def start(binary, data_dir, limit_bytes):
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
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError(
            "the database exited before the handshake: %s" % proc.stderr.read()[-2000:]
        )
    return proc


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


def close(proc):
    try:
        proc.stdin.close()
        proc.wait(timeout=120)
    except Exception:
        proc.kill()


def directory_size(path):
    """The size of a directory tree, in bytes."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


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
    parser.add_argument("--rows", type=int, default=1000000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--limit-mb", type=int, default=2048)
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args()

    limit_bytes = args.limit_mb * 1024 * 1024
    os.environ["MVDB_LIMIT_BYTES"] = str(limit_bytes)

    work = tempfile.mkdtemp(prefix="mvdb-limit-")
    data_dir = os.path.join(work, "data")
    out = {
        "rows": args.rows,
        "dim": args.dim,
        "text": args.text,
        "limit_mb": args.limit_mb,
        "phases": {},
    }
    body = "x" * args.text if args.text else ""
    try:
        # The write phase.
        proc = start(args.binary, data_dir, limit_bytes)
        try:
            if args.text:
                ask(
                    proc,
                    "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                    "body VARCHAR(1024), v VECTOR(%d));" % args.dim,
                )
            else:
                ask(
                    proc,
                    "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
                    "v VECTOR(%d));" % args.dim,
                )
            for first in range(0, args.rows, args.batch):
                values = []
                for row_id in range(first, min(first + args.batch, args.rows)):
                    vec = ",".join(
                        str((row_id * (i + 1)) % 100) for i in range(args.dim)
                    )
                    if args.text:
                        values.append(
                            "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
                        )
                    else:
                        values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
                ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
            ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            probe = ",".join(str(i % 100) for i in range(args.dim))
            answer = ask(
                proc, "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
            )
            assert answer.get("ok") is True, answer
            assert len(answer.get("rows", [])) == 10, answer
        finally:
            close(proc)
        out["phases"]["write_and_query"] = "answered"

        # The restart phase: the recovery and the rebuild of the indexes
        # happen before the handshake.
        proc = start(args.binary, data_dir, limit_bytes)
        try:
            answer = ask(proc, "SELECT id FROM t LIMIT 1;")
            assert answer.get("ok") is True, answer
        finally:
            close(proc)
        out["phases"]["restart"] = "answered"
        out["data_directory_mib"] = round(directory_size(data_dir) / 1048576.0, 1)
        out["data_larger_than_the_budget"] = (
            out["data_directory_mib"] > args.limit_mb
        )
    except RuntimeError as e:
        out["error"] = str(e)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if len(out["phases"]) == 2 else 1


if __name__ == "__main__":
    sys.exit(main())
