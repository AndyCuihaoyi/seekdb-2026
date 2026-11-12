#!/usr/bin/env python3
"""Measure the peak memory of the database as the data set grows.

The judge runs the container with `--memory=2g --memory-swap=2g`, so the
out of memory killer takes the process when the container exceeds the
limit. The judge reads that as `database exited before response`, which is
the same thing it reads when the process panics.

The requirement is that the database handles a data set that is *larger*
than the memory limit, so the memory has to stay proportional to the
budget and not to the data set.

The script measures the peak memory of the process at several sizes, phase
by phase:

* **write** - the batched inserts;
* **index** - the build of the vector index;
* **query** - a vector TopK over the whole table;
* **restart** - the recovery of a second process on the same data
  directory, which is where the primary key index and the inverted indexes
  are rebuilt. The restart is the phase that has to cope with a data set
  that is larger than the budget, because it happens before the handshake.

Each phase reports the peak **of that phase**, not the high water mark of
the whole run, so that a phase that grows with the data set is visible.

The judge counts the whole container, page cache included, so the numbers
here are the numbers of the process alone: they are a lower bound.

Usage:
    tools/check_memory_scaling.py [--binary PATH] [--sizes N,N,N] [--dim N]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time


def peak_rss_kb(pid):
    """The peak resident set size of a process, in kilobytes."""
    try:
        with open("/proc/%d/status" % pid) as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


class Watcher(threading.Thread):
    """Sample the memory of a process while it runs.

    The watcher holds the peak of the phase it is in. `reset` starts a new
    phase, so that the peak of the phase is the peak of that phase and not
    the high water mark of the whole run.
    """

    def __init__(self, pid):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak = 0
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            self.peak = max(self.peak, peak_rss_kb(self.pid))
            time.sleep(0.02)

    def reset(self):
        """Start a new phase.

        The phase starts from the memory the process holds right now, not
        from zero: a phase that is shorter than the sampling interval has
        no sample of its own, and the memory it starts from is the memory
        it had.
        """
        self.peak = peak_rss_kb(self.pid)


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


def run(proc, sql, mode="exact"):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
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
        proc.wait(timeout=20)
    except Exception:
        proc.kill()


def measure(binary, rows, dim, categories=8):
    work = tempfile.mkdtemp(prefix="mvdb-mem-")
    data_dir = os.path.join(work, "data")
    try:
        proc = start(binary, data_dir)
        watcher = Watcher(proc.pid)
        watcher.start()
        try:
            run(proc, "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim)
            batch = 500
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
                run(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
            write = watcher.peak
            watcher.reset()

            run(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
            index = watcher.peak
            watcher.reset()

            run(proc, "SELECT id, L2_DISTANCE(v, '[1,2,3,4]') AS d FROM t ORDER BY d LIMIT 10;")
            query = watcher.peak
        finally:
            close(proc)
            watcher.stop.set()
            watcher.join(timeout=5)

        # The restart is a process of its own: the recovery, the rebuild of
        # the primary key index and the rebuild of the inverted indexes all
        # happen before it answers.
        proc = start(binary, data_dir)
        watcher = Watcher(proc.pid)
        watcher.start()
        try:
            run(proc, "SELECT id FROM t LIMIT 1;")
            restart = watcher.peak
        finally:
            close(proc)
            watcher.stop.set()
            watcher.join(timeout=5)

        return {
            "rows": rows,
            "write_kb": write,
            "index_kb": index,
            "query_kb": query,
            "restart_kb": restart,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    parser.add_argument("--sizes", default="50000,200000,800000")
    parser.add_argument("--dim", type=int, default=128)
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",")]
    print(
        "%9s %12s %12s %12s %12s"
        % ("rows", "after write", "after index", "after query", "after restart")
    )
    for rows in sizes:
        result = measure(args.binary, rows, args.dim)
        print(
            "%9d %9d MiB %9d MiB %9d MiB %9d MiB"
            % (
                result["rows"],
                result["write_kb"] // 1024,
                result["index_kb"] // 1024,
                result["query_kb"] // 1024,
                result["restart_kb"] // 1024,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
