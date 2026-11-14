#!/usr/bin/env python3
"""Time the start-up of the database after a crash.

The public driver has no kill directive, but the formal package does: the
README of the package names "已确认提交在进程强杀后保留" and "恢复途中
再次退出仍能恢复" as formal requirements, and the protocol of the judge
has a `crash()` that sends SIGKILL.

**The start-up after a crash is a different start-up.** The clean start
reads an empty log and returns from the recovery at once; the start-up
after a crash replays the log, and the log of a crashed database holds
every record of every transaction that was not yet folded into the data
files.

The four phases of the start-up are printed to stderr by the database
itself, so this script reads them out of the log of the process and
reports them next to the wall clock of the handshake.

Usage:
    tools/probe_crash_startup.py --binary PATH [--rows N] [--text T]
                                 [--dim D] [--batch N] [--cpus N]
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


PHASE_RE = re.compile(r"startup phase (\w+) ([0-9.]+)s")


def start(binary, data_dir, cpus, stderr_path=None):
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir
    inner = "exec %s --data-dir {data_dir}" % binary
    argv = ["sh", "-c", inner]
    if cpus:
        argv = ["taskset", "-c", "0,%d" % (cpus - 1)] + argv
    # The diagnostics go to a file rather than to a pipe: a pipe whose
    # buffer fills up would block the database, and a blocked database is
    # exactly the failure this script looks for.
    stderr_handle = open(stderr_path, "ab") if stderr_path else subprocess.PIPE
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_handle,
        env=env,
        text=True,
        bufsize=1,
        # The process gets a session of its own, so that the kill of the
        # crash test does not reach the shell that runs this script.
        start_new_session=True,
    )
    proc.stderr_file = stderr_handle
    started = time.monotonic()
    ready = proc.stdout.readline()
    if not ready:
        raise RuntimeError(
            "the database exited before the handshake: %s" % proc.stderr.read()[-2000:]
        )
    return proc, time.monotonic() - started


def ask(proc, sql):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": "exact"}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database exited: %s" % proc.stderr.read()[-1500:])
    return json.loads(line)


def drain_stderr(proc):
    """Read everything the process wrote to stderr, without blocking."""
    try:
        import fcntl
        fd = proc.stderr.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        data = proc.stderr.read() or ""
        return data
    except Exception:
        return ""


def write_data(binary, data_dir, rows, text, dim, batch, cpus):
    """Fill the table, and leave the log with the records of the last
    transactions, so that the start-up after the crash has work to do."""
    proc, _ = start(binary, data_dir, cpus)
    try:
        ask(
            proc,
            "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
            "body VARCHAR(%d), v VECTOR(%d));" % (max(text, 1), dim),
        )
        body = "x" * text if text else ""
        for first in range(0, rows, batch):
            values = []
            for row_id in range(first, min(first + batch, rows)):
                vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(dim))
                if text:
                    values.append(
                        "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
                    )
                else:
                    values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
            ask(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
        ask(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=120)
        except Exception:
            proc.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    work = tempfile.mkdtemp(prefix="mvdb-crash-")
    data_dir = os.path.join(work, "data")
    stderr_path = os.path.join(work, "stderr.log")
    report = {"args": vars(args)}
    try:
        # The data is written and the process is closed cleanly, so that the
        # log is folded into the data files. The crash is done on a second
        # run, with the transactions still in the log.
        write_data(
            args.binary, data_dir, args.rows, args.text, args.dim, args.batch, args.cpus
        )

        # A crash with an open transaction: the rows of the transaction are
        # in the log but not committed, so the recovery has to skip them.
        proc, _ = start(args.binary, data_dir, args.cpus, stderr_path)
        try:
            ask(proc, "BEGIN;")
            values = ",".join(
                "(%d,'c9','[%s]')"
                % (i, ",".join(str((i * (j + 1)) % 100) for j in range(args.dim)))
                for i in range(args.rows, args.rows + 1000)
            )
            ask(proc, "INSERT INTO t VALUES " + values + ";")
            # The transaction is never committed: the crash happens with it
            # open, so the recovery has to roll it back.
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=60)
        except Exception:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except Exception:
                pass
        report["crash_done"] = True

        # The start-up after the crash: the phases are printed to stderr.
        proc, handshake = start(args.binary, data_dir, args.cpus, stderr_path)
        try:
            report["restart_handshake_s"] = round(handshake, 3)
            answer = ask(proc, "SELECT id FROM t LIMIT 1;")
            report["query_after_crash_ok"] = answer.get("ok") is True
        finally:
            try:
                proc.stdin.close()
                proc.wait(timeout=120)
            except Exception:
                proc.kill()

        with open(stderr_path, "r", encoding="utf-8", errors="replace") as handle:
            stderr_text = handle.read()
        phases = {}
        for name, value in PHASE_RE.findall(stderr_text):
            phases[name] = float(value)
        report["phases_s"] = phases
        report["phase_sum_s"] = round(sum(phases.values()), 3)
        report["stderr_tail"] = stderr_text[-2000:]

        # The same, but the process is killed a second time during the
        # recovery: the recovery has to be idempotent.
        proc, _ = start(args.binary, data_dir, args.cpus, stderr_path)
        try:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=60)
        except Exception:
            pass
        proc, handshake = start(args.binary, data_dir, args.cpus, stderr_path)
        try:
            report["second_restart_handshake_s"] = round(handshake, 3)
            answer = ask(proc, "SELECT id FROM t LIMIT 1;")
            report["query_after_second_crash_ok"] = answer.get("ok") is True
        finally:
            try:
                proc.stdin.close()
                proc.wait(timeout=120)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
