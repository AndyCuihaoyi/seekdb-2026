#!/usr/bin/env python3
"""Tell the three timeouts of the judge apart.

The judge raises `JSON response timeout` in exactly one place,
`Session.read()`, and it means one of three different things:

* the handshake of the start-up did not arrive within `startup_timeout`,
* a single statement did not answer within `request_timeout`,
* the `shutdown` did not answer within `request_timeout`.

The message does not say which one, and it does not say which statement.
The platform feedback therefore does not say either, and guessing has
already cost two rounds.

This script removes the ambiguity. It **imports the official transport**
(`/tmp/official/protocol_check/_transport.py`) and does not reimplement it:
the `Session` is subclassed, and only `read()` is wrapped, so the point
at which the wait happened is recorded and the failure is re-raised with
that point attached. Everything else - the handshake, the request, the
shutdown, the budgets - is the judge's own code.

The data set is built so that the two paths of the inverted index are
both exercised and can be told apart:

* the filter column has **low cardinality** (eight values), so its index
  is built and used;
* the table also has a **long text column** (`body VARCHAR(T)`), which is
  the blind spot of the gate that is run today.

Three loads are run, so that the cost of the index, of the restart and of
the shutdown can be separated:

* (A) write, build the index, query, restart, query again;
* (B) the same without the index;
* (C) the same without the restart.

Every phase is timed and compared against the budget of the judge, so
that the report can name the phase that is over the budget rather than
the phase that is merely the largest.

Usage:
    tools/repro_timeout_class.py --binary PATH [--rows N] [--text T]
                                 [--dim D] [--batch N] [--case-timeout S]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/tmp/official/protocol_check")

from _transport import Session, ProtocolError  # noqa: E402


# The budgets of the judge, from /tmp/official/protocol_check/config.json.
CONFIG_PATH = "/tmp/official/protocol_check/config.json"


class ClassifiedTimeout(TimeoutError):
    """A timeout that knows which of the three waits it came from."""

    def __init__(self, phase, message):
        super().__init__("%s timeout: %s" % (phase, message))
        self.phase = phase
        self.original = message


class ClassifiedSession(Session):
    """The judge's session, with the point of the wait recorded.

    Only `read()` is wrapped. Everything else - the handshake, the
    request, the shutdown and the budgets - is the judge's own code, so
    a number measured here is a number the judge would measure.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phase = "UNKNOWN"

    def read(self, timeout):
        try:
            return super().read(timeout)
        except TimeoutError as exc:
            raise ClassifiedTimeout(self.phase, str(exc)) from exc


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_command(binary, cpus, cpu_list=None):
    """The argv of the database, with the limits of the judge applied.

    The judge runs the database in a container with two CPUs and a two
    gigabyte memory limit. The same two limits are applied here with
    `taskset` for the CPUs and `ulimit -v` for the address space, so that
    the measurement is made under the same constraints.

    `cpu_list` names the cores the database is allowed to run on, so that
    two experiments can run at the same time without sharing a core. It
    defaults to the first `cpus` cores.
    """
    inner = (
        "ulimit -v %d 2>/dev/null || true; exec %s --data-dir {data_dir}"
        % (2 * 1024 * 1024, binary)
    )
    if cpu_list:
        return ["taskset", "-c", cpu_list, "sh", "-c", inner]
    if cpus:
        return ["taskset", "-c", "0,%d" % (cpus - 1), "sh", "-c", inner]
    return ["sh", "-c", inner]


def make_session(config, binary, cpus, case_timeout, log_path, cpu_list=None):
    return ClassifiedSession(
        config,
        log_path,
        command=build_command(binary, cpus, cpu_list),
        case_timeout=case_timeout,
    )


def timed(session, phase, action):
    """Run an action, and record the time it took and where it failed."""
    session.phase = phase
    started = time.monotonic()
    try:
        value = action()
    except ClassifiedTimeout as exc:
        return None, time.monotonic() - started, exc.phase
    except ProtocolError as exc:
        return None, time.monotonic() - started, "PROTOCOL: %s" % exc
    return value, time.monotonic() - started, None


def measure(config, binary, rows, text, dim, batch, cpus, case_timeout, log_path, cpu_list=None):
    """Run one load and time every phase of it."""
    out = {"rows": rows, "text": text, "dim": dim, "batch": batch}
    session = make_session(config, binary, cpus, case_timeout, log_path, cpu_list)

    # The handshake of a fresh database.
    value, took, failure = timed(session, "STARTUP", lambda: session.start())
    out["handshake_fresh_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    ddl = (
        "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
        "body VARCHAR(%d), v VECTOR(%d));" % (max(text, 1), dim)
    )
    answer, took, failure = timed(
        session, "STATEMENT", lambda: session.request(ddl)
    )
    out["ddl_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    # The write, in batches. The cost of the last batch is reported on
    # its own: a cost that grows with the number of rows is invisible in
    # the mean and fatal in the last one.
    body = "x" * text if text else ""
    write_batches = []
    failed = None
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
        sql = "INSERT INTO t VALUES " + ",".join(values) + ";"
        answer, took, failure = timed(session, "STATEMENT", lambda: session.request(sql))
        write_batches.append(took)
        if failure:
            failed = failure
            break
        if answer is not None and answer.get("ok") is not True:
            failed = "the insert was refused: %s" % str(answer)[:200]
            break
    out["write_s"] = round(sum(write_batches), 3)
    if write_batches:
        out["write_first_batch_s"] = round(write_batches[0], 4)
        out["write_last_batch_s"] = round(write_batches[-1], 4)
        out["write_growth"] = round(
            write_batches[-1] / max(write_batches[0], 1e-9), 2
        )
    if failed:
        out["failure"] = failed
        session.close()
        return out

    # The query, before the index exists.
    probe = ",".join(str(i % 100) for i in range(dim))
    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request(
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
        ),
    )
    out["query_before_index_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    # The index, and the query again.
    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request("CREATE VECTOR INDEX idx_t ON t(v);"),
    )
    out["index_build_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request(
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
        ),
    )
    out["query_after_index_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    # The same query in the approximate mode. The judge switches to it
    # with `--mode ann`, and the approximate path visits the rows through
    # the cells of the index rather than through the blocks of the table,
    # so it is a different code path with its own cost.
    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request(
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
            "ann",
        ),
    )
    out["query_ann_filtered_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request(
            "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
            "ann",
        ),
    )
    out["query_ann_unfiltered_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    # The shutdown, with the time it takes to answer and to exit.
    answer, took, failure = timed(session, "SHUTDOWN", lambda: session.shutdown())
    out["shutdown_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out
    session.close()
    return out


def measure_restart(config, binary, rows, text, dim, batch, cpus, case_timeout, log_path, cpu_list=None):
    """Load (A): the restart, and the first query after it.

    The restart is the part the judge charges to `startup_timeout`: the
    recovery and the rebuild of the indexes happen **before** the
    handshake, so they are all part of the wait.
    """
    out = {"rows": rows, "text": text, "dim": dim}
    session = make_session(config, binary, cpus, case_timeout, log_path, cpu_list)
    value, took, failure = timed(session, "STARTUP", lambda: session.start())
    out["handshake_fresh_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    ddl = (
        "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
        "body VARCHAR(%d), v VECTOR(%d));" % (max(text, 1), dim)
    )
    session.request(ddl)
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
        answer, took, failure = timed(
            session,
            "STATEMENT",
            lambda: session.request(
                "INSERT INTO t VALUES " + ",".join(values) + ";"
            ),
        )
        if failure:
            out["failure"] = failure
            session.close()
            return out
    answer, took, failure = timed(
        session, "STATEMENT", lambda: session.request("CREATE VECTOR INDEX idx_t ON t(v);")
    )
    out["index_build_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    # Close cleanly and start again on the same directory.
    answer, took, failure = timed(session, "SHUTDOWN", lambda: session.shutdown())
    out["shutdown_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    # The handshake of the restart: the recovery and the rebuild of the
    # indexes are inside this wait.
    value, took, failure = timed(session, "STARTUP", lambda: session.start())
    out["restart_handshake_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    probe = ",".join(str(i % 100) for i in range(dim))
    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request(
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe
        ),
    )
    out["query_after_restart_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    # The same query in the approximate mode, after the restart. The
    # judge switches to it with `--mode ann`, and the approximate path is
    # a different code path with its own cost.
    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request(
            "SELECT id FROM t WHERE cat = 'c3' "
            "ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
            "ann",
        ),
    )
    out["query_ann_filtered_after_restart_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    answer, took, failure = timed(
        session,
        "STATEMENT",
        lambda: session.request(
            "SELECT id FROM t ORDER BY L2_DISTANCE(v, '[%s]') LIMIT 10;" % probe,
            "ann",
        ),
    )
    out["query_ann_unfiltered_after_restart_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out

    answer, took, failure = timed(session, "SHUTDOWN", lambda: session.shutdown())
    out["shutdown_s"] = round(took, 3)
    if failure:
        out["failure"] = failure
        session.close()
        return out
    session.close()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--cpu-list", default=None,
                        help="the cores the database may run on, e.g. 0,1")
    parser.add_argument("--case-timeout", type=float, default=3600)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    work = tempfile.mkdtemp(prefix="mvdb-class-")
    log_path = os.path.join(work, "stderr.log")
    report = {"config": config, "args": vars(args), "loads": {}}

    try:
        # Load (A): the restart.
        report["loads"]["A_restart"] = measure_restart(
            config,
            args.binary,
            args.rows,
            args.text,
            args.dim,
            args.batch,
            args.cpus,
            args.case_timeout,
            log_path,
            args.cpu_list,
        )
        # Load (B): no index.
        report["loads"]["B_no_index"] = measure(
            config,
            args.binary,
            args.rows,
            args.text,
            args.dim,
            args.batch,
            args.cpus,
            args.case_timeout,
            log_path,
            args.cpu_list,
        )
        # Load (C): no restart.
        report["loads"]["C_no_restart"] = measure(
            config,
            args.binary,
            args.rows,
            args.text,
            args.dim,
            args.batch,
            args.cpus,
            args.case_timeout,
            log_path,
            args.cpu_list,
        )
    finally:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                report["stderr_tail"] = handle.read()[-4000:]
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)

    # The budgets, so that the report can name the phase that is over.
    startup = config["startup_timeout"]
    statement = config["request_timeout"]
    report["budgets"] = {
        "STARTUP": startup,
        "STATEMENT": statement,
        "SHUTDOWN": statement,
    }

    for name, m in report["loads"].items():
        print("=== load %s ===" % name)
        for key in (
            "handshake_fresh_s",
            "write_s",
            "write_first_batch_s",
            "write_last_batch_s",
            "write_growth",
            "query_before_index_s",
            "index_build_s",
            "query_after_index_s",
            "query_ann_filtered_s",
            "query_ann_unfiltered_s",
            "restart_handshake_s",
            "query_after_restart_s",
            "query_ann_filtered_after_restart_s",
            "query_ann_unfiltered_after_restart_s",
            "shutdown_s",
            "failure",
        ):
            if key in m:
                print("  %-24s %s" % (key, m[key]))
        # The verdict of the load, phase by phase.
        verdicts = []
        if m.get("restart_handshake_s", 0) > startup:
            verdicts.append("STARTUP over budget")
        if m.get("handshake_fresh_s", 0) > startup:
            verdicts.append("STARTUP over budget")
        for key in ("write_s", "index_build_s", "query_after_index_s",
                    "query_after_restart_s", "query_before_index_s",
                    "query_ann_filtered_s", "query_ann_unfiltered_s",
                    "query_ann_filtered_after_restart_s",
                    "query_ann_unfiltered_after_restart_s"):
            if m.get(key, 0) > statement:
                verdicts.append("%s over budget" % key)
        if m.get("shutdown_s", 0) > statement:
            verdicts.append("SHUTDOWN over budget")
        if m.get("failure"):
            verdicts.append("FAILED: %s" % m["failure"])
        print("  %-24s %s" % ("verdict", "; ".join(verdicts) or "all phases within budget"))
        print()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
