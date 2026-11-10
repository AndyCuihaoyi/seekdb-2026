#!/usr/bin/env python3
"""Check the database under the exact resource limits of the judge.

The official checker runs the container with

    --cpus=2 --memory=2g --memory-swap=2g --pids-limit=128

and the official README says that the graded package contains cases whose
data is *generated* by parameters that are kept back, and that the
implementation has to cope with data that is larger than the memory limit.

So the question is not "how fast" but "does the peak memory stay inside
2 GiB when the data does not fit into 2 GiB".

The measurement is taken from inside the container, because the score
counts the whole cgroup, not the process alone.

    bash tools/bench_container.sh --rows 1000000 --dim 128 --queries 100
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time


def docker(*args, check=True):
    return subprocess.run(["docker", *args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=3600, check=check)


def peak_of(container):
    """Read the peak memory of the container's cgroup."""
    out = docker("exec", container, "cat", "/sys/fs/cgroup/memory.peak").stdout
    return int(out.decode().strip())


def io_of(container):
    out = docker("exec", container, "cat", "/sys/fs/cgroup/io.stat").stdout
    total = 0
    for line in out.decode().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in ("read_bytes", "write_bytes"):
            total += int(parts[2])
    return total


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
    parser.add_argument("--image", default="mvdb-jsonl:local")
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--cpus", default="2")
    args = parser.parse_args()

    name = "mvdb-limit-probe"
    volume = tempfile.mkdtemp(prefix="mvdb-limit-")
    docker("rm", "--force", "--volumes", name, check=False)

    # The container is started with the very limits of the judge, and the
    # standard streams are the ones the judge talks to: the image's own
    # entry point is the database, so no second process is started.
    proc = subprocess.Popen(
        ["docker", "run", "--name", name, "--interactive",
         "--network=none",
         "--cpus=" + args.cpus,
         "--memory=" + args.memory,
         "--memory-swap=" + args.memory,
         "--pids-limit=128",
         "--cap-drop=ALL", "--security-opt=no-new-privileges",
         "--read-only",
         "-u", "%d:%d" % (os.getuid(), os.getgid()),
         "--mount", "type=bind,src=%s,dst=/data" % volume,
         args.image],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1)

    try:
        handshake = proc.stdout.readline()
        if handshake.strip() != '{"type":"ready"}':
            print("the handshake is wrong: %r" % handshake)
            return 1

        sequence = [0]

        def ask(sql, mode="exact"):
            sequence[0] += 1
            request = {"id": sequence[0], "op": "execute", "sql": sql,
                       "search_mode": mode}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())

        answer = ask("CREATE TABLE big (id BIGINT PRIMARY KEY, g BIGINT, e VECTOR(%d));" % args.dim)
        if answer.get("ok") is not True:
            print("the create failed: %s" % answer)
            return 1

        # The write, in batches of 2000 rows.
        started = time.monotonic()
        written = 0
        batch = 2000
        while written < args.rows:
            size = min(batch, args.rows - written)
            values = ", ".join(
                "(%d, %d, '[%s]')" % (
                    written + i, i % 2,
                    ",".join(str((i * 7 + j) % 101) for j in range(args.dim)))
                for i in range(size))
            answer = ask("INSERT INTO big VALUES %s;" % values)
            if answer.get("ok") is not True:
                print("the insert failed at %d rows: %s" % (written, answer))
                return 1
            written += size
            if written % 20000 == 0:
                print("  written %d rows, peak %.0f MiB" % (
                    written, peak_of(name) / 1048576.0))
        write_seconds = time.monotonic() - started
        write_peak = peak_of(name)

        # The query, in exact mode, which is the mode that scans everything.
        started = time.monotonic()
        for i in range(args.queries):
            answer = ask(
                "SELECT id, L2_DISTANCE(e, '[%s]') AS d FROM big "
                "WHERE g = 0 ORDER BY d LIMIT 10;" % ",".join(["1"] * args.dim))
            if answer.get("ok") is not True:
                print("the query failed: %s" % answer)
                return 1
        query_seconds = time.monotonic() - started
        query_peak = peak_of(name)

        # A restart, to see what the recovery costs on a large data set.
        proc.stdin.write(json.dumps({"id": 999999, "op": "shutdown"}) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
        proc.wait(timeout=600)

        started = time.monotonic()
        proc = subprocess.Popen(
            ["docker", "run", "--name", name + "-2", "--interactive",
             "--network=none",
             "--cpus=" + args.cpus,
             "--memory=" + args.memory,
             "--memory-swap=" + args.memory,
             "--pids-limit=128",
             "--cap-drop=ALL", "--security-opt=no-new-privileges",
             "--read-only",
             "-u", "%d:%d" % (os.getuid(), os.getgid()),
             "--mount", "type=bind,src=%s,dst=/data" % volume,
             args.image],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        proc.stdout.readline()
        restart_seconds = time.monotonic() - started
        restart_peak = peak_of(name + "-2")

        answer = ask("SELECT id FROM big WHERE id = %d;" % (args.rows - 1))
        survived = answer.get("rows") == [[args.rows - 1]]

        limit = int(args.memory.rstrip("g")) * 1024 * 1024 * 1024
        peak = max(write_peak, query_peak, restart_peak)
        print()
        print("rows              %d x %d dimensions" % (args.rows, args.dim))
        print("data directory    %.1f MiB" % (directory_size(volume) / 1048576.0))
        print("write             %.1f s" % write_seconds)
        print("query             %.1f s for %d queries" % (query_seconds, args.queries))
        print("restart           %.1f s" % restart_seconds)
        print("peak memory       %.0f MiB of %.0f MiB" % (peak / 1048576.0, limit / 1048576.0))
        print("the rows survive  %s" % survived)
        print("inside the limit  %s" % (peak <= limit))
        return 0 if (peak <= limit and survived) else 1
    finally:
        try:
            proc.stdin.write(json.dumps({"id": 999998, "op": "shutdown"}) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        docker("rm", "--force", "--volumes", name, check=False)
        docker("rm", "--force", "--volumes", name + "-2", check=False)


if __name__ == "__main__":
    sys.exit(main())
