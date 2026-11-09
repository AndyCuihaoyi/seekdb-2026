#!/usr/bin/env python3
"""Self test of MiniVectorDB against a Python reference implementation.

The reference is deliberately simple: it keeps the rows in memory and
computes the L2 distance with the ordinary Euclidean formula. The database
under test is asked the same questions, and the two answers are compared.

The comparison is not limited to the identifiers: the distances are compared
as well, because a wrong distance with a right identifier would still be a
wrong answer for a query that returns the distance.

The data set is generated with a fixed seed, so a failure can always be
reproduced.

# The protocol

The database speaks a MySQL `mysqltest` style protocol on stdout: a result
set is a header line followed by one line per row, and an error is a single
line `ERROR <code>: <message>`. A statement that produces no result set
produces no output at all.

The protocol has no end-of-result marker, so the test cannot tell "the
statement is done" from "the statement produced nothing". Every statement is
therefore followed by a *synchronisation query* whose output is a single
known line, and the test reads until it sees that line. Everything before it
belongs to the statement under test.

Usage:
    tools/selftest.py [--binary PATH] [--data-dir PATH] [--verbose]

The script exits with a non-zero status when a check fails.
"""

import argparse
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

# The tolerance of the distance comparison. The database computes the
# distance in single precision and the reference in double precision, so the
# two differ slightly.
DIST_TOLERANCE = 1e-4

# The number of rows of the generated data set.
ROW_COUNT = 2000

# The dimension of the generated vectors.
DIM = 16


class Failure(Exception):
    """A check that did not hold."""


class Db:
    """A running instance of the database under test."""

    def __init__(self, binary, data_dir, mode="exact", nlist=32, nprobe=None):
        env = dict(os.environ)
        env["MINIVECDB_DATA_DIR"] = data_dir
        env["MINIVECDB_MODE"] = mode
        env["MINIVECDB_OUTPUT_FORMAT"] = "tsv"
        env["MINIVECDB_READY_SINK"] = "stderr"
        env["MINIVECDB_ANN_NLIST"] = str(nlist)
        if nprobe is not None:
            env["MINIVECDB_ANN_NPROBE"] = str(nprobe)
        else:
            env.pop("MINIVECDB_ANN_NPROBE", None)
        self.proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
        )
        # Wait for the readiness token.
        line = self.proc.stderr.readline()
        if "ready" not in line:
            raise Failure("the database did not report readiness: %r" % line)

        # The synchronisation token. It is unique, so that it cannot be
        # confused with a line of a result set.
        self.token = "SYNC-%d-%s" % (
            os.getpid(),
            "".join(random.Random().choices("0123456789", k=8)),
        )
        # The synchronisation table has a name of its own, so that several
        # instances can use the same data directory.
        self.sync_table = "__mvdb_sync_%d_%s" % (
            os.getpid(),
            "".join(random.Random().choices("0123456789", k=8)),
        )
        self.sync_sql = "SELECT tok FROM %s;" % self.sync_table

        # The synchronisation table is created without the synchronisation
        # helper, because the helper needs the table to exist.
        self.send(
            "CREATE TABLE %s (id BIGINT PRIMARY KEY, tok VARCHAR(64));"
            % self.sync_table
        )
        self.send("INSERT INTO %s VALUES (1,'%s');" % (self.sync_table, self.token))

    # ---------------------------------------------------------------- talking

    def send(self, sql):
        """Write one statement to the database without reading its answer."""
        self.proc.stdin.write(sql.rstrip() + "\n")
        self.proc.stdin.flush()

    def run(self, sql):
        """Run one statement and return the lines it produced.

        The returned lines are the result set without its header, or the
        error line. An empty list means the statement produced no output.
        """
        self.send(sql)
        self.send(self.sync_sql)

        lines = []
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                raise Failure(
                    "the database closed its output while running %r" % sql
                )
            line = raw.rstrip("\n")
            if line == self.token:
                break
            lines.append(line)
        # The last line before the token is the header of the synchronisation
        # result set, which belongs to the protocol and not to the statement.
        if lines and lines[-1] == "tok":
            lines.pop()
        return lines

    def query(self, sql, expected_rows):
        """Run a query and return its rows.

        The number of rows is stated by the caller, because the protocol
        carries no row count.
        """
        lines = self.run(sql)
        if len(lines) == 1 and lines[0].startswith("ERROR "):
            raise Failure("the query failed: %s" % lines[0])
        if len(lines) != expected_rows + 1:
            raise Failure(
                "the query returned %d lines, expected %d rows plus a header"
                % (len(lines), expected_rows)
            )
        return lines[1:]

    def exec(self, sql):
        """Run a statement that produces no result set.

        An error is reported as a failure, so that a broken statement cannot
        pass unnoticed.
        """
        lines = self.run(sql)
        for line in lines:
            if line.startswith("ERROR "):
                raise Failure("the statement failed: %s" % line)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def kill(self):
        """Terminate the process abruptly, like a crash."""
        self.proc.kill()
        self.proc.wait(timeout=10)


# ------------------------------------------------------------------ reference


def vector_literal(values):
    """Render a vector the way the SQL parser expects it."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def l2(a, b):
    """The L2 distance of two vectors, in double precision."""
    return math.sqrt(sum((x - y) * (x - y) for x, y in zip(a, b)))


def topk_truth(rows, query, k, predicate=None):
    """The reference answer of a TopK query.

    `rows` is a list of `(pk, category, vector)` triples. The predicate is
    applied first, so that a filtered query is answered over the filtered
    set - the whole point of the filter.
    """
    candidates = []
    for pk, category, vec in rows:
        if predicate is not None and not predicate(category):
            continue
        candidates.append((l2(vec, query), pk))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[:k]


def generate_rows(seed=20261109, count=ROW_COUNT, dim=DIM):
    """Generate a reproducible data set."""
    rng = random.Random(seed)
    rows = []
    for pk in range(1, count + 1):
        vec = [rng.uniform(-10.0, 10.0) for _ in range(dim)]
        category = "cat%d" % (pk % 5)
        rows.append((pk, category, vec))
    return rows


def check(condition, message):
    if not condition:
        raise Failure(message)


def compare_topk(name, got, truth, tolerance=DIST_TOLERANCE):
    """Compare a database answer with the reference answer."""
    check(
        len(got) == len(truth),
        "%s: expected %d rows, got %d" % (name, len(truth), len(got)),
    )
    for i, ((gdist, gpk), (tdist, tpk)) in enumerate(zip(got, truth)):
        check(gpk == tpk, "%s: row %d has pk %d, expected %d" % (name, i, gpk, tpk))
        check(
            abs(gdist - tdist) <= tolerance * max(1.0, abs(tdist)),
            "%s: row %d has distance %r, expected %r" % (name, i, gdist, tdist),
        )


def parse_topk(lines):
    """Parse the answer of a TopK query into `(distance, pk)` pairs."""
    out = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 2:
            raise Failure("cannot parse the answer line %r" % line)
        out.append((float(parts[1]), int(parts[0])))
    return out


def topk_sql(table, query, k, where=None):
    """The TopK query used by the checks."""
    clause = " WHERE %s" % where if where else ""
    return (
        "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM %s%s "
        "ORDER BY distance LIMIT %d;" % (vector_literal(query), table, clause, k)
    )


# ------------------------------------------------------------------- the checks


def case_exact_topk(db, rows, rng):
    """The exact TopK equals the reference answer."""
    for _ in range(20):
        query = [rng.uniform(-10.0, 10.0) for _ in range(DIM)]
        k = rng.choice([1, 5, 10, 25])
        got = parse_topk(db.query(topk_sql("t", query, k), k))
        truth = topk_truth(rows, query, k)
        compare_topk("exact topk", got, truth)


def case_ann_topk(db, rows, rng):
    """The approximate TopK reaches the reference answer."""
    by_pk = {pk: vec for pk, _, vec in rows}
    for _ in range(20):
        query = [rng.uniform(-10.0, 10.0) for _ in range(DIM)]
        k = rng.choice([1, 5, 10, 25])
        got = parse_topk(db.query(topk_sql("t", query, k), k))
        truth = topk_truth(rows, query, k)
        truth_pks = [pk for _, pk in truth]
        found = [pk for _, pk in got]
        hits = len(set(found) & set(truth_pks))
        recall = hits / max(1, len(truth_pks))
        check(recall >= 0.95, "ann topk: recall %.3f is below 0.95" % recall)
        check(
            len(set(found)) == len(found),
            "ann topk: the answer contains a duplicated identifier",
        )
        # The distances must still be correct for the rows that were found.
        for dist, pk in got:
            expected = l2(by_pk[pk], query)
            check(
                abs(dist - expected) <= DIST_TOLERANCE * max(1.0, abs(expected)),
                "ann topk: pk %d has distance %r, expected %r" % (pk, dist, expected),
            )


def case_filtered_topk(db, rows, rng):
    """A filtered TopK equals the answer computed over the filtered set."""
    for _ in range(20):
        query = [rng.uniform(-10.0, 10.0) for _ in range(DIM)]
        k = rng.choice([1, 5, 10])
        category = "cat%d" % rng.randrange(5)
        got = parse_topk(
            db.query(topk_sql("t", query, k, where="cat='%s'" % category), k)
        )
        truth = topk_truth(rows, query, k, predicate=lambda c: c == category)
        compare_topk("filtered topk", got, truth)
        # The filtered set must not contain a row of another category: a
        # wrong implementation that filters after the TopK would return rows
        # of another category here.
        by_pk = {pk: (cat, vec) for pk, cat, vec in rows}
        for _, pk in got:
            cat = by_pk[pk][0]
            check(
                cat == category,
                "filtered topk: pk %d has category %s, expected %s"
                % (pk, cat, category),
            )


def case_ties(db):
    """Rows at the same distance come out in primary key order."""
    db.exec("CREATE TABLE ties (id BIGINT PRIMARY KEY, v VECTOR(2));")
    # Four rows at the same distance from the origin.
    for pk, vec in [
        (7, "[1,0]"),
        (3, "[-1,0]"),
        (5, "[0,1]"),
        (1, "[0,-1]"),
    ]:
        db.exec("INSERT INTO ties VALUES (%d,'%s');" % (pk, vec))
    sql = (
        "SELECT id, L2_DISTANCE(v,'[0,0]') AS distance FROM ties "
        "ORDER BY distance LIMIT 4;"
    )
    got = parse_topk(db.query(sql, 4))
    check(
        [pk for _, pk in got] == [1, 3, 5, 7],
        "ties: the answer is %r, expected the primary keys in ascending order"
        % [pk for _, pk in got],
    )
    for dist, _ in got:
        check(
            abs(dist - 1.0) <= DIST_TOLERANCE,
            "ties: distance %r, expected 1" % dist,
        )


def case_index_maintenance(db, rows, rng):
    """The index follows the changes of the data.

    After an insert, an update of a vector and a delete, the approximate
    answer must equal the answer computed over the rows that are visible at
    that moment.
    """
    db.exec("CREATE TABLE maint (id BIGINT PRIMARY KEY, cat VARCHAR(16), v VECTOR(4));")
    live = []
    rng2 = random.Random(4242)
    for pk in range(1, 201):
        vec = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
        cat = "cat%d" % (pk % 3)
        db.exec(
            "INSERT INTO maint VALUES (%d,'%s','%s');" % (pk, cat, vector_literal(vec))
        )
        live.append((pk, cat, vec))
    db.exec("CREATE VECTOR INDEX idx_maint ON maint(v);")

    # An insert after the index was created.
    new_vec = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
    db.exec("INSERT INTO maint VALUES (1001,'cat0','%s');" % vector_literal(new_vec))
    live.append((1001, "cat0", new_vec))

    # An update of a vector: the row moves to another cell of the index.
    updated_pk = 50
    updated_vec = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
    db.exec(
        "UPDATE maint SET v='%s' WHERE id=%d;" % (vector_literal(updated_vec), updated_pk)
    )
    live = [(pk, cat, updated_vec if pk == updated_pk else vec) for pk, cat, vec in live]

    # A delete.
    deleted_pk = 77
    db.exec("DELETE FROM maint WHERE id=%d;" % deleted_pk)
    live = [r for r in live if r[0] != deleted_pk]

    for _ in range(15):
        query = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
        k = 10
        got = parse_topk(db.query(topk_sql("maint", query, k), k))
        truth = topk_truth(live, query, k)
        compare_topk("index maintenance", got, truth)


def case_index_persistence(binary, data_dir, rows, rng):
    """The index survives a restart.

    The database is started again on the same data directory; the index has
    to be rebuilt before the database reports readiness, and the answer must
    not change.
    """
    def answers(db, label):
        for _ in range(10):
            query = [rng.uniform(-10.0, 10.0) for _ in range(DIM)]
            k = 10
            got = parse_topk(db.query(topk_sql("t", query, k), k))
            truth = topk_truth(rows, query, k)
            compare_topk(label, got, truth)

    db = Db(binary, data_dir, mode="ann")
    try:
        answers(db, "index persistence (before restart)")
    finally:
        db.close()

    # Start again on the same directory. The index has to be rebuilt from
    # the persisted records, and the answer must be the same.
    db = Db(binary, data_dir, mode="ann")
    try:
        answers(db, "index persistence (after restart)")
    finally:
        db.close()


def case_transaction(binary, data_dir, rng):
    """A query inside a transaction sees the changes of the transaction."""
    db = Db(binary, data_dir, mode="ann")
    try:
        db.exec("CREATE TABLE txn (id BIGINT PRIMARY KEY, v VECTOR(4));")
        live = []
        rng2 = random.Random(99)
        for pk in range(1, 101):
            vec = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
            db.exec("INSERT INTO txn VALUES (%d,'%s');" % (pk, vector_literal(vec)))
            live.append((pk, vec))
        db.exec("CREATE VECTOR INDEX idx_txn ON txn(v);")

        db.exec("BEGIN;")
        new_vec = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
        db.exec("INSERT INTO txn VALUES (1001,'%s');" % vector_literal(new_vec))
        live.append((1001, new_vec))
        upd_pk = 20
        upd_vec = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
        db.exec("UPDATE txn SET v='%s' WHERE id=%d;" % (vector_literal(upd_vec), upd_pk))
        live = [(pk, upd_vec if pk == upd_pk else v) for pk, v in live]
        del_pk = 30
        db.exec("DELETE FROM txn WHERE id=%d;" % del_pk)
        live = [r for r in live if r[0] != del_pk]

        # The answer inside the transaction must equal the answer over the
        # rows the transaction makes visible.
        for _ in range(10):
            query = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
            k = 10
            got = parse_topk(db.query(topk_sql("txn", query, k), k))
            truth = topk_truth([(pk, "c", v) for pk, v in live], query, k)
            compare_topk("transaction", got, truth)

        db.exec("COMMIT;")

        # After the commit the answer must be the same.
        for _ in range(5):
            query = [rng2.uniform(-10.0, 10.0) for _ in range(4)]
            k = 10
            got = parse_topk(db.query(topk_sql("txn", query, k), k))
            truth = topk_truth([(pk, "c", v) for pk, v in live], query, k)
            compare_topk("transaction (after commit)", got, truth)
    finally:
        db.close()


def case_exact_and_ann_agree(binary, data_dir, rows, rng):
    """The two paths answer the same question the same way.

    The database is asked the same queries in the exact mode and in the
    approximate mode; the two answers must be identical, because the pruning
    of the index is exact.
    """
    # The same queries are asked in both modes, so the answers can be
    # compared one by one.
    queries = []
    for _ in range(20):
        queries.append(([rng.uniform(-10.0, 10.0) for _ in range(DIM)], 10))

    answers = {}
    for mode in ("exact", "ann"):
        db = Db(binary, data_dir, mode=mode)
        try:
            got = []
            for query, k in queries:
                got.append(parse_topk(db.query(topk_sql("t", query, k), k)))
            answers[mode] = got
        finally:
            db.close()
    for i, (a, b) in enumerate(zip(answers["exact"], answers["ann"])):
        check(
            [pk for _, pk in a] == [pk for _, pk in b],
            "exact and approximate disagree at query %d: %r against %r"
            % (i, [pk for _, pk in a], [pk for _, pk in b]),
        )


# --------------------------------------------------------------------- the run


def populate(db, rows):
    """Load the generated data set into a fresh table."""
    db.exec("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(16), v VECTOR(%d));" % DIM)
    # The rows are inserted in batches, so that the test does not spend all
    # its time waiting for the pipe.
    batch = []
    for pk, category, vec in rows:
        batch.append("(%d,'%s','%s')" % (pk, category, vector_literal(vec)))
        if len(batch) >= 100:
            db.exec("INSERT INTO t VALUES " + ",".join(batch) + ";")
            batch = []
    if batch:
        db.exec("INSERT INTO t VALUES " + ",".join(batch) + ";")
    db.exec("CREATE VECTOR INDEX idx_t ON t(v);")


def run_cases(binary, data_dir, rows, rng, mode):
    """Run the cases that need a running database."""
    db = Db(binary, data_dir, mode=mode)
    failures = []
    try:
        populate(db, rows)
        cases = [
            ("exact topk", lambda db: case_exact_topk(db, rows, rng)),
            ("ann topk", lambda db: case_ann_topk(db, rows, rng)),
            ("filtered topk", lambda db: case_filtered_topk(db, rows, rng)),
            ("ties", case_ties),
            ("index maintenance", lambda db: case_index_maintenance(db, rows, rng)),
        ]
        for name, case in cases:
            try:
                case(db)
                print("ok    %s (%s)" % (name, mode))
            except Failure as e:
                failures.append((name, str(e)))
                print("FAIL  %s (%s): %s" % (name, mode, e))
            except Exception as e:  # noqa: BLE001
                failures.append((name, repr(e)))
                print("ERROR %s (%s): %r" % (name, mode, e))
    finally:
        db.close()
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary",
        default=None,
        help="the database binary (default: the release build in the crate)",
    )
    parser.add_argument("--data-dir", default=None, help="the data directory to use")
    parser.add_argument("--verbose", action="store_true", help="print every check")
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    binary = args.binary or os.path.join(
        root, "minivectordb", "target", "release", "minivectordb"
    )
    if not os.path.exists(binary):
        print("error: the binary %s does not exist" % binary, file=sys.stderr)
        print("       run 'cargo build --release --offline' first", file=sys.stderr)
        return 2

    temporary = None
    if args.data_dir is None:
        temporary = tempfile.mkdtemp(prefix="mvdb-selftest-")
        data_dir = temporary
    else:
        shutil.rmtree(args.data_dir, ignore_errors=True)
        os.makedirs(args.data_dir, exist_ok=True)
    data_dir = os.path.abspath(data_dir)

    rows = generate_rows()
    rng = random.Random(31337)

    failures = []
    try:
        # The exact mode, on a fresh data directory.
        exact_dir = os.path.join(data_dir, "exact")
        os.makedirs(exact_dir, exist_ok=True)
        failures += run_cases(binary, exact_dir, rows, rng, "exact")

        # The approximate mode, on a fresh data directory.
        ann_dir = os.path.join(data_dir, "ann")
        os.makedirs(ann_dir, exist_ok=True)
        failures += run_cases(binary, ann_dir, rows, rng, "ann")

        # The index has to survive a restart.
        try:
            case_index_persistence(binary, ann_dir, rows, rng)
            print("ok    index persistence")
        except Failure as e:
            failures.append(("index persistence", str(e)))
            print("FAIL  index persistence: %s" % e)
        except Exception as e:  # noqa: BLE001
            failures.append(("index persistence", repr(e)))
            print("ERROR index persistence: %r" % e)

        # The queries inside a transaction.
        try:
            case_transaction(binary, ann_dir, rng)
            print("ok    transaction")
        except Failure as e:
            failures.append(("transaction", str(e)))
            print("FAIL  transaction: %s" % e)
        except Exception as e:  # noqa: BLE001
            failures.append(("transaction", repr(e)))
            print("ERROR transaction: %r" % e)

        # The two paths have to agree.
        try:
            case_exact_and_ann_agree(binary, ann_dir, rows, rng)
            print("ok    exact and approximate agree")
        except Failure as e:
            failures.append(("exact and approximate agree", str(e)))
            print("FAIL  exact and approximate agree: %s" % e)
        except Exception as e:  # noqa: BLE001
            failures.append(("exact and approximate agree", repr(e)))
            print("ERROR exact and approximate agree: %r" % e)
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)

    print("")
    if failures:
        print("%d check(s) failed:" % len(failures))
        for name, message in failures:
            print("  - %s: %s" % (name, message))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
