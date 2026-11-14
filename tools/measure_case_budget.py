#!/usr/bin/env python3
"""Measure the **whole case** against the budget of the judge.

The judge charges every statement sixty seconds and every case six
hundred, and the two budgets are not independent: `Session.remaining()`
takes the **smaller** of the two, so when the budget of the case is nearly
spent the next statement is cut short, and the timeout it raises is the
very same `JSON response timeout` that a statement over its own budget
raises. A case that accumulates slowly therefore looks exactly like a
case with one slow statement, and the two cannot be told apart by the
message.

The experiments of this repository measured the second thing - a single
statement - and never the first. This script measures the first thing.

It **imports the official transport** (`/tmp/official/protocol_check/_transport.py`)
and does not reimplement it. The budgets are the ones of the judge and
they are **not** widened: `case_timeout` is 600, `request_timeout` is 60
and `startup_timeout` is 120, all of them read from the official
`config.json`. The `Session` is subclassed only to record the phase the
wait came from, and the wall clock of the **whole case** - from the
creation of the session to the shutdown - is the number the report is
about.

The statement sequence is the one of the public case
`vector_index_maintenance`, with the write in the batches of a hundred
rows that the rule of the platform names:

    CREATE TABLE -> INSERT x N (100 rows each) -> CREATE VECTOR INDEX ->
    UPDATE -> DELETE -> INSERT -> SELECT -> --restart -> SELECT ->
    --mode ann -> SELECT -> shutdown

Every phase is timed on its own, and the sum of the phases is compared
with the six hundred seconds of the case.

Usage:
    tools/measure_case_budget.py --binary PATH [--rows N] [--text T]
                                 [--dim D] [--filter] [--case-timeout S]
                                 [--output FILE]
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "/tmp/official/protocol_check")

from _transport import Session, ProtocolError  # noqa: E402

CONFIG_PATH = "/tmp/official/protocol_check/config.json"


class ClassifiedTimeout(TimeoutError):
    """A timeout that knows which of the waits it came from."""

    def __init__(self, phase, message):
        super().__init__("%s timeout: %s" % (phase, message))
        self.phase = phase
        self.original = message


class ClassifiedSession(Session):
    """The judge's session, with the point of the wait recorded.

    Only `read()` is wrapped. Everything else - the handshake, the
    request, the shutdown and the budgets - is the judge's own code, so a
    number measured here is a number the judge would measure.
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


# The marker of a wait that did not fail. `Session.start()` and
# `Session.shutdown()` return nothing, so the value of the action cannot
# tell a failure from a success and a marker is needed.
OK = object()


def build_command(binary, cpus):
    """The argv of the database, with the limits of the judge applied.

    The judge runs the database in a container with two CPUs and a two
    gigabyte memory limit. The same two limits are applied here with
    `taskset` for the CPUs and `ulimit -v` for the address space, so the
    measurement is made under the same constraints.
    """
    inner = (
        "ulimit -v %d 2>/dev/null || true; exec %s --data-dir {data_dir}"
        % (2 * 1024 * 1024, binary)
    )
    if cpus:
        return ["taskset", "-c", "0,%d" % (cpus - 1), "sh", "-c", inner]
    return ["sh", "-c", inner]


def timed(session, phase, action, out, key):
    """Run an action, and record the time it took on its own."""
    session.phase = phase
    started = time.monotonic()
    try:
        value = action()
    except ClassifiedTimeout as exc:
        out[key + "_s"] = round(time.monotonic() - started, 3)
        out["failure"] = exc.phase
        out["failure_message"] = str(exc)
        return None
    except ProtocolError as exc:
        out[key + "_s"] = round(time.monotonic() - started, 3)
        out["failure"] = "PROTOCOL"
        out["failure_message"] = str(exc)
        return None
    out[key + "_s"] = round(time.monotonic() - started, 3)
    if value is None:
        return OK
    return value


def prepare_statements(rows, text, dim, batch, use_filter):
    """The statements of the case, prepared **before** the clock starts.

    The judge reads the statements of a case from a **file**, so the cost of
    preparing them is not part of the case. The reproducer of this
    repository prepared them in memory, and the preparation of four
    million rows - forty thousand statements of a hundred rows each - cost
    more than a quarter of the budget of the case. That cost is an
    artefact of the reproducer, not a cost of the database, and a
    measurement that counts it measures the wrong thing.

    So the statements are built **first**, in a list, and the case then
    runs them one at a time. Writing them to a file instead would cost
    eleven gigabytes of disk at eight million rows, which the machine
    does not have, and it would not make the measurement any truer: the
    judge reads the statements of a case from a file too, and the reading
    of a file is not the cost of the database.
    """
    body = "x" * text if text else ""
    probe = ",".join(str(i % 100) for i in range(dim))

    statements = []
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
        statements.append("INSERT INTO docs VALUES " + ",".join(values) + ";")

    # The three write statements of the public case, in its order.
    statements.append("CREATE VECTOR INDEX docs_vec ON docs(embedding);")
    statements.append("UPDATE docs SET embedding = '[%s]' WHERE id = 1;" % probe)
    statements.append("DELETE FROM docs WHERE id = 2;")
    statements.append(
        "INSERT INTO docs VALUES (%d,'c0','x','[%s]');" % (rows, probe)
    )

    # The query of the public case: `ORDER BY distance LIMIT 10`, with no
    # filter. The `--filter` switch measures the shape of the earlier
    # version of the amplified case, for comparison.
    where = " WHERE category = 'c3'" if use_filter else ""
    query = (
        "SELECT id, L2_DISTANCE(embedding, '[%s]') AS distance FROM docs"
        "%s ORDER BY distance LIMIT 10;" % (probe, where)
    )
    statements.append(query)
    statements.append("--restart")
    statements.append(query)
    statements.append("--mode ann")
    statements.append(query)
    return statements


def measure(config, binary, rows, text, dim, batch, cpus, case_timeout,
            use_filter, log_path, work_dir):
    """Run one whole case and time every phase of it.

    The number the report is about is `case_total_s`: the wall clock from
    the creation of the session to the end, against the six hundred
    seconds of the judge.
    """
    out = {
        "rows": rows, "text": text, "dim": dim, "batch": batch,
        "filter": use_filter,
    }
    session = ClassifiedSession(
        config, log_path,
        command=build_command(binary, cpus),
        case_timeout=case_timeout,
    )

    # The statements are prepared **before** the clock of the case starts,
    # because the judge reads them from a file.
    statements = prepare_statements(rows, text, dim, batch, use_filter)

    case_started = time.monotonic()

    value = timed(session, "STARTUP", lambda: session.start(), out, "handshake_fresh")
    if value is None:
        out["case_total_s"] = round(time.monotonic() - case_started, 3)
        session.close()
        return out

    ddl = (
        "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
        "content VARCHAR(%d), embedding VECTOR(%d));" % (max(text, 1), dim)
    )
    value = timed(session, "STATEMENT", lambda: session.request(ddl), out, "ddl")
    if value is None:
        out["case_total_s"] = round(time.monotonic() - case_started, 3)
        session.close()
        return out

    # The write, in the batches of a hundred rows that the rule of the
    # platform names.
    write_batches = []
    failed = False
    for sql in statements:
        if not sql.startswith("INSERT"):
            break
        answer = timed(session, "STATEMENT", lambda: session.request(sql), out,
                       "write_batch_%d" % len(write_batches))
        write_batches.append(out.get("write_batch_%d_s" % len(write_batches)))
        if answer is None:
            failed = True
            break
        if answer.get("ok") is not True:
            out["failure"] = "the insert was refused: %s" % str(answer)[:200]
            failed = True
            break
    if failed:
        out["case_total_s"] = round(time.monotonic() - case_started, 3)
        session.close()
        return out
    out["write_s"] = round(sum(write_batches), 3)
    out["write_batches"] = len(write_batches)

    # The three write statements of the public case, in its order.
    probe = ",".join(str(i % 100) for i in range(dim))
    for sql, key in (
        ("CREATE VECTOR INDEX docs_vec ON docs(embedding);", "index_build"),
        ("UPDATE docs SET embedding = '[%s]' WHERE id = 1;" % probe, "update"),
        ("DELETE FROM docs WHERE id = 2;", "delete"),
        ("INSERT INTO docs VALUES (%d,'c0','x','[%s]');" % (rows, probe), "insert"),
    ):
        answer = timed(session, "STATEMENT", lambda: session.request(sql), out, key)
        if answer is None:
            out["case_total_s"] = round(time.monotonic() - case_started, 3)
            session.close()
            return out

    # The query of the public case: `ORDER BY distance LIMIT 10`, with no
    # filter. The `--filter` switch measures the shape of the earlier
    # version of the amplified case, for comparison.
    where = " WHERE category = 'c3'" if use_filter else ""
    query = (
        "SELECT id, L2_DISTANCE(embedding, '[%s]') AS distance FROM docs"
        "%s ORDER BY distance LIMIT 10;" % (probe, where)
    )

    answer = timed(session, "STATEMENT", lambda: session.request(query), out,
                   "query_exact_before_restart")
    if answer is None:
        out["case_total_s"] = round(time.monotonic() - case_started, 3)
        session.close()
        return out

    # The restart: the recovery and the rebuild of the indexes happen
    # before the handshake, so they are all inside the wait.
    timed(session, "SHUTDOWN", lambda: session.shutdown(), out, "shutdown_1")
    value = timed(session, "STARTUP", lambda: session.start(), out, "restart_handshake")
    if value is None:
        out["case_total_s"] = round(time.monotonic() - case_started, 3)
        session.close()
        return out

    answer = timed(session, "STATEMENT", lambda: session.request(query), out,
                   "query_exact_after_restart")
    if answer is None:
        out["case_total_s"] = round(time.monotonic() - case_started, 3)
        session.close()
        return out

    # The approximate mode. The judge switches to it with `--mode ann`,
    # and the approximate path is a different code path with its own cost.
    answer = timed(session, "STATEMENT",
                   lambda: session.request(query, "ann"), out, "query_ann")
    if answer is None:
        out["case_total_s"] = round(time.monotonic() - case_started, 3)
        session.close()
        return out

    timed(session, "SHUTDOWN", lambda: session.shutdown(), out, "shutdown_2")

    out["case_total_s"] = round(time.monotonic() - case_started, 3)
    session.close()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--rows", type=int, default=4000000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=100,
                        help="the batch of the write, the rule of the platform "
                             "names a hundred rows")
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--case-timeout", type=float, default=600,
                        help="the budget of the case; the default is the one "
                             "of the judge and it is not to be widened")
    parser.add_argument("--filter", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    config = load_config()
    report = {
        "budgets": {
            "startup_timeout": config["startup_timeout"],
            "request_timeout": config["request_timeout"],
            "case_timeout": config["case_timeout"],
        },
        "args": vars(args),
        "loads": [],
    }

    work = tempfile.mkdtemp(prefix="mvdb-budget-")
    log_path = os.path.join(work, "stderr.log")
    try:
        out = measure(
            config, args.binary, args.rows, args.text, args.dim, args.batch,
            args.cpus, args.case_timeout, args.filter, log_path, work,
        )
        report["loads"].append(out)
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                report["stderr_tail"] = handle.read()[-2000:]
        except OSError:
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)

    for out in report["loads"]:
        print("=== the case of %d rows ===" % out["rows"])
        for key, value in out.items():
            if key in ("rows", "text", "dim", "batch", "filter"):
                continue
            if key.startswith("write_batch_") and not key.endswith("_s"):
                continue
            print("  %-34s %s" % (key, value))
        total = out.get("case_total_s", 0)
        budget = report["budgets"]["case_timeout"]
        print("  %-34s %.1f s of %.0f s (%.0f%%)"
              % ("the case against its budget", total, budget,
                 100.0 * total / budget))
        print()

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print("the report was written to %s" % args.output)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
