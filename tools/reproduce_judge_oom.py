#!/usr/bin/env python3
"""Reproduce the admission gate failure the way the judge sees it.

The judge runs the container with `--memory=2g --memory-swap=2g`, so the
out of memory killer takes the process when the container exceeds the
limit. The judge reads a dead process as `database exited before
response`, which is the same thing it reads when the process panics.

This script runs the database inside a cgroup of its own with the same
limit the judge uses, drives it over the standard input and output
protocol, and reports what happened. The two ways the run can go wrong
are told apart:

* the process was killed - the kernel sent it a signal, and the cgroup
  records an out of memory event;
* the process answered - every statement came back and the answers were
  checked.

Usage:
    tools/reproduce_judge_oom.py [--rows N] [--dim N] [--limit-mb N]
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

CGROUP_V2 = "/sys/fs/cgroup"


def make_cgroup(name, limit_bytes):
    """Create a cgroup with a memory limit and move this process into it.

    The process has to be in the parent cgroup for the child to be
    created, so the caller moves itself in and the database inherits the
    membership.
    """
    # The cgroup of this process is named by its path in the hierarchy.
    try:
        with open("/proc/self/cgroup") as f:
            line = f.read().strip()
        path = line.split("::", 1)[1]
    except (OSError, IndexError):
        path = "/"
    parent = os.path.join(CGROUP_V2, path.lstrip("/"))
    try:
        with open(os.path.join(parent, "cgroup.controllers")) as f:
            controllers = f.read().split()
        if "memory" not in controllers:
            return None
        with open(os.path.join(parent, "cgroup.subtree_control"), "w") as f:
            f.write("+memory")
    except OSError as e:
        sys.stderr.write("cannot prepare the cgroup: %s\n" % e)
        return None

    child = os.path.join(parent, name)
    try:
        os.makedirs(child, exist_ok=True)
        with open(os.path.join(child, "memory.max"), "w") as f:
            f.write(str(limit_bytes))
        with open(os.path.join(child, "cgroup.procs"), "w") as f:
            f.write(str(os.getpid()))
        return child
    except OSError as e:
        sys.stderr.write("cannot create the cgroup: %s\n" % e)
        return None


def read_cgroup(cgroup):
    out = {}
    for name in ("memory.peak", "memory.current", "memory.max", "memory.events"):
        path = os.path.join(cgroup, name)
        try:
            with open(path) as f:
                out[name.split(".")[1]] = f.read().strip()
        except OSError:
            out[name.split(".")[1]] = None
    return out


def run(binary, data_dir, rows, dim, categories=8, batch=500):
    """Drive the database the way the judge does."""
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        bufsize=1,
    )

    def send(sql, mode="exact"):
        request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("database exited before response")
        return json.loads(line)

    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError("database exited before the handshake")
    assert json.loads(ready) == {"type": "ready"}, ready

    send("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim)
    for first in range(0, rows, batch):
        values = []
        for row_id in range(first, min(first + batch, rows)):
            values.append(
                "(%d,'c%d','[%d,%s]')"
                % (
                    row_id,
                    row_id % categories,
                    row_id % 1000,
                    ",".join(str((row_id * (i + 1)) % 100) for i in range(dim - 1)),
                )
            )
        send("INSERT INTO t VALUES " + ",".join(values) + ";")
    send("CREATE VECTOR INDEX idx_t ON t(v);")
    probe = ",".join(str(i % 100) for i in range(dim))
    response = send("SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe)
    assert response.get("ok") is True, response
    assert len(response.get("rows", [])) == 10, response
    return {"rows": rows, "answered": True}


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
    parser.add_argument("--rows", type=int, default=800000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--limit-mb", type=int, default=2048)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-oom-")
    data_dir = os.path.join(work, "data")
    cgroup = make_cgroup("mvdb-oom-%d" % os.getpid(), args.limit_mb * 1024 * 1024)
    outcome = {"rows": args.rows, "limit_mb": args.limit_mb}
    try:
        try:
            outcome.update(run(args.binary, data_dir, args.rows, args.dim))
        except RuntimeError as e:
            outcome["answered"] = False
            outcome["error"] = str(e)
        if cgroup:
            outcome["cgroup"] = read_cgroup(cgroup)
            events = outcome["cgroup"].get("events") or ""
            outcome["oom_killed"] = "oom_kill" in events
    finally:
        if cgroup:
            try:
                os.rmdir(cgroup)
            except OSError:
                pass
        shutil.rmtree(work, ignore_errors=True)

    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 0 if outcome.get("answered") else 1


if __name__ == "__main__":
    sys.exit(main())
