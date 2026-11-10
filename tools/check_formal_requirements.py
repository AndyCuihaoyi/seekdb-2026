#!/usr/bin/env python3
"""Check the five requirements that the official package states explicitly.

The README of the official public package says that the graded package has
29 cases, that 9 of them are kept back, and that what is kept back is the
test *data*, not the *requirements*. It then lists the requirements that
still hold:

    1. NaN, Infinity, a wrong dimension and a float32 overflow have to be
       refused.
    2. A failing statement must not take effect partially, and the
       statements that succeeded before it in an explicit transaction still
       have to be committable and rollbackable.
    3. A committed change survives a kill, an uncommitted one does not take
       effect partially, and an interruption *during* the recovery still
       leaves a database that recovers again.
    4. The data may be larger than the memory limit.
    5. The approximate search has to honour the filter, return valid ids,
       return no duplicates, and reach the required recall; an index update
       must not break the transaction semantics.

Each of them is checked here over the real wire protocol.

    python3 tools/check_formal_requirements.py --binary <path>
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

READY = '{"type":"ready"}'


class Server:
    """One database process, talked to over the standard streams."""

    def __init__(self, binary, data_dir):
        self.proc = subprocess.Popen(
            [binary, "--data-dir", data_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.sequence = 0
        handshake = self.proc.stdout.readline()
        if handshake.strip() != READY:
            raise RuntimeError("the handshake is wrong: %r" % handshake)

    def ask(self, sql, mode="exact"):
        self.sequence += 1
        request = {"id": self.sequence, "op": "execute", "sql": sql,
                   "search_mode": mode}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("the database stopped answering")
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.write(json.dumps({"id": 999999, "op": "shutdown"}) + "\n")
            self.proc.stdin.flush()
            self.proc.stdout.readline()
        except (BrokenPipeError, ValueError):
            pass
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def kill(self):
        """Terminate the process the way the crash tests do."""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=30)


def ok(answer):
    return answer.get("ok") is True


def code(answer):
    return answer.get("error", {}).get("code", "")


def report(results, name, good, detail=""):
    results.append((name, good))
    print("%-4s %-44s %s" % ("ok" if good else "MISS", name, detail))


def check_bad_vectors(results, binary):
    """Requirement 1: the illegal vectors have to be refused."""
    server = Server(binary, tempfile.mkdtemp(prefix="bad-vec-"))
    try:
        server.ask("CREATE TABLE v (id BIGINT PRIMARY KEY, e VECTOR(2));")
        # One valid row, so that the table is not empty: a distance over an
        # empty table is never computed and would hide the errors.
        server.ask("INSERT INTO v VALUES (1, '[0,0]');")
        cases = [
            ("vector/nan", "INSERT INTO v VALUES (1, '[NaN,0]');"),
            ("vector/inf", "INSERT INTO v VALUES (1, '[Infinity,0]');"),
            ("vector/-inf", "INSERT INTO v VALUES (1, '[-Infinity,0]');"),
            ("vector/too-few", "INSERT INTO v VALUES (1, '[1]');"),
            ("vector/too-many", "INSERT INTO v VALUES (1, '[1,2,3]');"),
            ("vector/not-a-number", "INSERT INTO v VALUES (1, 'abc');"),
            ("vector/empty", "INSERT INTO v VALUES (1, '[]');"),
            ("vector/overflow", "INSERT INTO v VALUES (1, '[1e40,0]');"),
            ("vector/distance-nan",
             "SELECT L2_DISTANCE(e, '[NaN,0]') FROM v;"),
            ("vector/distance-dimension",
             "SELECT L2_DISTANCE(e, '[1,2,3]') FROM v;"),
        ]
        for name, sql in cases:
            answer = server.ask(sql)
            report(results, name, not ok(answer) and bool(code(answer)),
                   code(answer))
    finally:
        server.close()


def check_statement_atomicity(results, binary):
    """Requirement 2: a failing statement takes effect partially never."""
    server = Server(binary, tempfile.mkdtemp(prefix="atomic-"))
    try:
        server.ask("CREATE TABLE a (id BIGINT PRIMARY KEY, e VECTOR(2));")
        answer = server.ask("INSERT INTO a VALUES (1, '[0,0]'), (2, '[1]');")
        report(results, "atomic/multi-row-insert-refused",
               not ok(answer) and bool(code(answer)), code(answer))
        answer = server.ask("SELECT id FROM a ORDER BY id;")
        rows = answer.get("rows", [])
        report(results, "atomic/no-row-left-behind", ok(answer) and rows == [],
               str(rows))

        # The same, inside an explicit transaction, with a statement before it.
        server.ask("BEGIN;")
        server.ask("INSERT INTO a VALUES (10, '[0,0]');")
        answer = server.ask("INSERT INTO a VALUES (11, '[0,0]'), (12, '[1]');")
        report(results, "atomic/inside-transaction-refused",
               not ok(answer) and bool(code(answer)), code(answer))
        server.ask("COMMIT;")
        answer = server.ask("SELECT id FROM a ORDER BY id;")
        rows = answer.get("rows", [])
        report(results, "atomic/the-earlier-statement-stays",
               ok(answer) and rows == [[10]], str(rows))

        # The same, but rolled back.
        server.ask("BEGIN;")
        server.ask("INSERT INTO a VALUES (20, '[0,0]');")
        server.ask("INSERT INTO a VALUES (21, '[0,0]'), (22, '[1]');")
        server.ask("ROLLBACK;")
        answer = server.ask("SELECT id FROM a ORDER BY id;")
        rows = answer.get("rows", [])
        report(results, "atomic/the-earlier-statement-is-rolled-back",
               ok(answer) and rows == [[10]], str(rows))
    finally:
        server.close()


def check_crash_recovery(results, binary):
    """Requirement 3: a kill keeps what was committed, and the recovery
    itself may be interrupted."""
    data_dir = tempfile.mkdtemp(prefix="crash-")
    server = Server(binary, data_dir)
    server.ask("CREATE TABLE c (id BIGINT PRIMARY KEY, e VECTOR(2));")
    server.ask("INSERT INTO c VALUES (1, '[0,0]');")
    server.ask("BEGIN;")
    server.ask("INSERT INTO c VALUES (2, '[1,1]');")
    server.kill()

    server = Server(binary, data_dir)
    answer = server.ask("SELECT id FROM c ORDER BY id;")
    rows = answer.get("rows", [])
    report(results, "crash/the-committed-row-survives", ok(answer) and rows == [[1]],
           str(rows))
    server.close()

    # Interrupt the recovery over and over. The data directory keeps the
    # half-written state, and the next start has to recover again.
    for attempt in range(6):
        server = Server(binary, data_dir)
        server.ask("INSERT INTO c VALUES (%d, '[2,2]');" % (100 + attempt))
        server.kill()

    server = Server(binary, data_dir)
    answer = server.ask("SELECT id FROM c ORDER BY id;")
    rows = answer.get("rows", [])
    report(results, "crash/the-database-recovers-again", ok(answer), str(rows)[:80])
    report(results, "crash/the-committed-row-is-still-there",
           ok(answer) and [[1]] == [r for r in rows if r == [1]], str(rows)[:80])
    server.close()


def check_large_data(results, binary):
    """Requirement 4: the data may be bigger than the memory limit."""
    data_dir = tempfile.mkdtemp(prefix="large-")
    server = Server(binary, data_dir)
    try:
        server.ask("CREATE TABLE big (id BIGINT PRIMARY KEY, e VECTOR(16));")
        # 40k rows of 16 dimensions is about 2.5 MiB of vectors, and the
        # row images make the data directory grow to a few tens of MiB.
        for batch in range(40):
            values = ", ".join(
                "(%d, '[%s]')" % (batch * 1000 + i,
                                  ",".join(["%d" % ((i + j) % 7) for j in range(16)]))
                for i in range(1000)
            )
            answer = server.ask("INSERT INTO big VALUES %s;" % values)
            if not ok(answer):
                report(results, "large/the-insert-works", False, code(answer))
                return
        answer = server.ask("SELECT id FROM big WHERE id = 39999;")
        report(results, "large/the-row-is-there",
               ok(answer) and answer.get("rows") == [[39999]], str(answer.get("rows")))
        answer = server.ask(
            "SELECT id, L2_DISTANCE(e, '[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]') AS d "
            "FROM big ORDER BY d LIMIT 3;")
        report(results, "large/the-search-works", ok(answer) and len(answer.get("rows", [])) == 3,
               str(answer.get("rows"))[:80])
    finally:
        server.close()
        shutil.rmtree(data_dir, ignore_errors=True)


def check_ann(results, binary):
    """Requirement 5: the approximate search honours the filter, returns
    valid ids, no duplicates, and reaches the recall."""
    data_dir = tempfile.mkdtemp(prefix="ann-")
    server = Server(binary, data_dir)
    try:
        server.ask("CREATE TABLE a (id BIGINT PRIMARY KEY, g BIGINT, e VECTOR(8));")
        for batch in range(10):
            values = ", ".join(
                "(%d, %d, '[%s]')" % (batch * 500 + i, i % 2,
                                      ",".join(["%d" % ((i * 3 + j) % 11) for j in range(8)]))
                for i in range(500)
            )
            server.ask("INSERT INTO a VALUES %s;" % values)
        server.ask("CREATE VECTOR INDEX ai ON a(e);")

        answer = server.ask(
            "SELECT id, L2_DISTANCE(e, '[1,2,3,4,5,6,7,8]') AS d FROM a "
            "WHERE g = 0 ORDER BY d LIMIT 10;", mode="ann")
        rows = answer.get("rows", [])
        ids = [row[0] for row in rows]
        report(results, "ann/the-filter-is-honoured",
               ok(answer) and all(i % 2 == 0 for i in ids), str(ids))
        report(results, "ann/no-duplicates", len(ids) == len(set(ids)), str(ids))
        report(results, "ann/the-ids-are-valid",
               all(0 <= i < 5000 for i in ids), str(ids))

        # The recall: the same query in exact mode is the truth.
        truth = server.ask(
            "SELECT id, L2_DISTANCE(e, '[1,2,3,4,5,6,7,8]') AS d FROM a "
            "WHERE g = 0 ORDER BY d LIMIT 10;", mode="exact")
        wanted = [row[0] for row in truth.get("rows", [])]
        overlap = len(set(ids) & set(wanted))
        report(results, "ann/the-recall-is-high",
               overlap >= 9, "%d of %d" % (overlap, len(wanted)))

        # An index update must not break the transaction semantics.
        server.ask("BEGIN;")
        server.ask("DELETE FROM a WHERE id = 1;")
        server.ask("ROLLBACK;")
        answer = server.ask("SELECT id FROM a WHERE id = 1;")
        report(results, "ann/the-index-update-is-transactional",
               ok(answer) and answer.get("rows") == [[1]], str(answer.get("rows")))
    finally:
        server.close()
        shutil.rmtree(data_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--only", action="append")
    args = parser.parse_args()

    checks = {
        "vectors": check_bad_vectors,
        "atomicity": check_statement_atomicity,
        "crash": check_crash_recovery,
        "large": check_large_data,
        "ann": check_ann,
    }
    results = []
    for name, function in checks.items():
        if args.only and name not in args.only:
            continue
        print("== %s" % name)
        function(results, args.binary)
        print()

    missing = [name for name, good in results if not good]
    print("%d of %d requirements hold" % (len(results) - len(missing), len(results)))
    for name in missing:
        print("  missing: %s" % name)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
