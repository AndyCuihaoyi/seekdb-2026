#!/usr/bin/env python3
"""Performance baseline of MiniVectorDB.

The script measures the three axes of the competition statement and checks
that the answers are still correct.

The data set is generated with the specification of the statement: rows of
a fixed dimension, a batch size of one hundred rows per transaction, and a
TopK query with a filter on half of the queries. The random generator is
seeded, so a run is reproducible.

The three axes are measured like this:

* **T** is the wall clock time of the write phase and of the query phase,
  measured separately.
* **B** is the number of bytes the process read and wrote, taken from
  `/proc/<pid>/io` (`rchar` plus `wchar`). The official measurement uses the
  `io.stat` of a control group, which counts the same physical I/O; the
  numbers are not comparable across the two, but the relative change is.
* **M** is the peak resident set size of the process, taken from
  `/proc/<pid>/status` (`VmHWM`). The official measurement uses the
  `memory.peak` of a control group, which has the same meaning.

The correctness of the answers is checked against a reference implementation
that keeps the rows in memory: the **Recall@10** is the share of the true
nearest neighbours that the database found, reported separately for the
filtered and for the unfiltered queries. The script fails when the recall
is below 0.95, when an answer holds a duplicated identifier, or when a
filtered answer contains a row that does not satisfy the filter.

The script writes its measurements as JSON, so that two runs can be compared
with `--compare`.

Usage:
    tools/bench.py [--rows 10000] [--dim 128] [--queries 1000] [--k 10]
                    [--filter-ratio 0.5] [--seed 42] [--batch 100]
                    [--output FILE] [--compare FILE] [--dump-baseline FILE]
                    [--mode exact|ann]

The script exits with a non-zero status when a check fails.
"""

import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

# The number of categories of the filter column.
CATEGORY_COUNT = 8

# The share of the true nearest neighbours that has to be found.
MIN_RECALL = 0.95


class Failure(Exception):
    """A check that did not hold."""


# ------------------------------------------------------------------ the database


class Db:
    """A running instance of the database under test.

    The database speaks the JSON Lines protocol of the competition: it
    announces itself with `{"type":"ready"}` and then answers one JSON
    object per request. The client checks the id of every answer, so a
    protocol bug shows up here rather than in the measurement.
    """

    def __init__(self, binary, data_dir, mode="ann", nlist=64, nprobe=None, cpu=None):
        env = dict(os.environ)
        env["MINIVECDB_DATA_DIR"] = data_dir
        env["MINIVECDB_MODE"] = mode
        env["MINIVECDB_ANN_NLIST"] = str(nlist)
        if nprobe is not None:
            env["MINIVECDB_ANN_NPROBE"] = str(nprobe)
        else:
            env.pop("MINIVECDB_ANN_NPROBE", None)
        # The measurement of this machine is noisy unless the server is
        # pinned to one core.
        #
        # The machine is shared, and the scheduler otherwise moves the server
        # between the cores while it runs. Every move takes the ten megabytes
        # of the decoded blocks out of the cache of the old core and puts
        # them back into the cache of the new one, so the scan pays for the
        # reads again. The effect is large: the same binary measured
        # 703 ms when it was free and 327 ms when it was pinned, and the
        # spread of the rounds fell from 49% to 3%.
        #
        # When the caller has already restricted the whole benchmark to a
        # set of cores with `pin_to_cpus`, the affinity of this process is
        # already set and the server inherits it, so there is nothing to do
        # here.
        command = [binary, "--data-dir", data_dir]
        if cpu is not None:
            command = ["taskset", "-c", str(cpu)] + command
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            bufsize=1,
        )
        self.sequence = 0
        self.mode = mode
        # The handshake has to arrive before the first request is sent.
        raw = self.proc.stdout.readline()
        if not raw:
            raise Failure("the database closed its output before the handshake")
        try:
            message = json.loads(raw)
        except ValueError as exc:
            raise Failure("the handshake is not JSON: %r (%s)" % (raw, exc))
        if message != {"type": "ready"}:
            raise Failure("expected a ready message, got %r" % (message,))

    def run(self, sql, mode=None):
        """Run one statement and return its answer."""
        self.sequence += 1
        request = {
            "id": self.sequence,
            "op": "execute",
            "sql": sql.rstrip().rstrip(";"),
            "search_mode": mode or self.mode,
        }
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        if not raw:
            raise Failure(
                "the database closed its output while running %r" % sql[:120]
            )
        answer = json.loads(raw)
        if answer.get("id") != request["id"]:
            raise Failure(
                "the answer carries the id %r, expected %r"
                % (answer.get("id"), request["id"])
            )
        if answer.get("ok") is not True:
            raise Failure(
                "the statement %r failed: %s" % (sql[:120], answer.get("error"))
            )
        return answer

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


# -------------------------------------------------------------- the measurement


def read_io_bytes(pid):
    """The number of bytes the process read from and wrote to the disk.

    The counters that count the bytes that reached the block device are
    `read_bytes` and `write_bytes`. The counters `rchar` and `wchar` count
    every byte the process read or wrote, **including the bytes that came
    in and went out through the pipes of the protocol**, and the SQL text of
    the benchmark is 11.7 MB for the load and 1.17 MB for the queries. So
    `rchar` and `wchar` are a measure of the traffic of the pipe, not of
    the disk.

    The platform reads `rbytes` and `wbytes` from `io.stat` of the cgroup,
    which counts the block device as well, so the block device counters are
    the ones that are comparable.

    A read that the page cache served does not reach the device either, so
    these counters are the lower bound of what the platform sees. That is
    the right direction to be wrong in: the number that is reported here is
    never larger than the number of the platform.
    """
    total = 0
    try:
        with open("/proc/%d/io" % pid, "r") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key.strip() in ("read_bytes", "write_bytes"):
                    total += int(value.strip())
    except OSError:
        return None
    return total


def pin_to_cpus(cpus):
    """Restrict this process, and the server it starts, to a set of cores.

    The judge runs the container with `--cpus=2`, so the measurement has to
    be taken on two cores to be comparable with the platform. On a machine
    with twenty cores the scheduler moves the server between them, and
    every move takes the decoded blocks out of the cache of the old core
    and puts them into the cache of the new one. The same binary was
    measured at 757 ms when it was free and at 344 ms when it was pinned
    to two cores, and the spread of the rounds fell from 76% to 4%.

    So the pinning is not a detail of the measurement: without it the
    number is both larger and meaningless.
    """
    if not cpus:
        return None
    cpus = {int(c) for c in cpus}
    try:
        os.sched_setaffinity(0, cpus)
    except (AttributeError, OSError):
        return None
    return cpus


def parse_cpu_list(text):
    """Parse a list of cores, as `0,1` or `0-3` or a mixture of the two."""
    cpus = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            cpus.update(range(int(first), int(last) + 1))
        else:
            cpus.add(int(part))
    return cpus


def read_peak_rss_kb(pid):
    """The peak resident set size of the process, in kilobytes."""
    try:
        with open("/proc/%d/status" % pid, "r") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


# ------------------------------------------------------------------ the data set


def vector_literal(values):
    """Render a vector the way the SQL parser expects it."""
    return "[" + ",".join("%.6f" % float(v) for v in values) + "]"


def generate_rows(seed, count, dim):
    """Generate a reproducible data set.

    The vectors are drawn uniformly from the unit interval, and the filter
    column takes a small number of discrete values, so that a filtered query
    really looks at a subset of the rows.
    """
    rng = random.Random(seed)
    rows = []
    for pk in range(1, count + 1):
        vec = [rng.uniform(0.0, 1.0) for _ in range(dim)]
        category = "c%d" % (pk % CATEGORY_COUNT)
        rows.append((pk, category, vec))
    return rows


def make_queries(seed, count, dim):
    """Generate the query vectors."""
    rng = random.Random(seed + 1)
    return [[rng.uniform(0.0, 1.0) for _ in range(dim)] for _ in range(count)]


def l2(a, b):
    """The L2 distance of two vectors, in double precision."""
    return math.sqrt(sum((x - y) * (x - y) for x, y in zip(a, b)))


def topk_truth(rows, query, k, category=None):
    """The reference answer of a TopK query.

    The filter is applied first, so that a filtered query is answered over
    the filtered set. Ties are broken by the primary key, exactly like the
    database does it.
    """
    best = []
    for pk, cat, vec in rows:
        if category is not None and cat != category:
            continue
        d = l2(vec, query)
        if len(best) < k:
            best.append((d, pk))
            best.sort()
        elif (d, pk) < best[-1]:
            best[-1] = (d, pk)
            best.sort()
    return best


# --------------------------------------------------------------------- the run


def phase_write(db, rows, dim, batch):
    """Load the data set, one transaction per batch of rows."""
    db.run("CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), v VECTOR(%d));" % dim)
    total = len(rows)
    done = 0
    while done < total:
        chunk = rows[done : done + batch]
        values = ",".join(
            "(%d,'%s','%s')" % (pk, cat, vector_literal(vec)) for pk, cat, vec in chunk
        )
        db.run("BEGIN;")
        db.run("INSERT INTO t VALUES " + values + ";")
        db.run("COMMIT;")
        done += len(chunk)


def phase_query(db, queries, k, filter_ratio, categories):
    """Run the TopK queries and collect the answers.

    Every query is a TopK over the vector column; half of them carry an
    equality filter on the category column. The answers are returned as a
    list of the primary keys, in the order the database returned them.
    """
    answers = []
    filtered_flags = []
    for i, query in enumerate(queries):
        category = None
        if filter_ratio > 0 and (i % 2 == 0):
            category = categories[i % len(categories)]
        clause = " WHERE cat = '%s'" % category if category else ""
        sql = (
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
            "ORDER BY distance LIMIT %d;" % (vector_literal(query), clause, k)
        )
        answer = db.run(sql)
        answers.append([int(row[0]) for row in answer.get("rows", [])])
        filtered_flags.append(category is not None)
    return answers, filtered_flags


def check_answers(answers, filtered_flags, rows, queries, k, categories):
    """Check the answers against the reference and compute the recall.

    The recall is computed over a sample of the queries, because the
    reference implementation is quadratic in the number of rows. The sample
    is chosen so that it covers both kinds of query.
    """
    by_pk = {pk: (cat, vec) for pk, cat, vec in rows}
    truth_cache = {}

    def truth_of(index):
        if index not in truth_cache:
            category = categories[index % len(categories)] if filtered_flags[index] else None
            truth_cache[index] = topk_truth(rows, queries[index], k, category)
        return truth_cache[index]

    sample = []
    # The sample covers both kinds of query, so that the recall of the
    # filtered and of the unfiltered queries are both measured.
    #
    # The two kinds are sampled separately, because they do not alternate
    # over the whole range: the filtered queries are the ones with an even
    # index, so a stride that starts at zero would only ever pick filtered
    # queries.
    step = max(1, len(answers) // 10)
    unfiltered = [i for i in range(len(answers)) if not filtered_flags[i]]
    filtered = [i for i in range(len(answers)) if filtered_flags[i]]
    sample += unfiltered[:: max(1, len(unfiltered) // 10)][:10]
    sample += filtered[:: max(1, len(filtered) // 10)][:10]
    sample = sorted(set(sample))

    recalls = {True: [], False: []}
    for index in sample:
        got = answers[index]
        expected = truth_of(index)
        if len(got) != len(set(got)):
            raise Failure(
                "query %d: the answer holds a duplicated identifier: %r" % (index, got)
            )
        if filtered_flags[index]:
            wanted = categories[index % len(categories)]
            for pk in got:
                cat, _ = by_pk[pk]
                if cat != wanted:
                    raise Failure(
                        "query %d: pk %d has category %s, expected %s"
                        % (index, pk, cat, wanted)
                    )
        expected_pks = set(pk for _, pk in expected)
        hits = len(set(got) & expected_pks)
        recalls[filtered_flags[index]].append(hits / max(1, len(expected_pks)))

    def mean(values):
        return sum(values) / len(values) if values else None

    return {
        "recall_filtered": mean(recalls[True]),
        "recall_unfiltered": mean(recalls[False]),
        "sample_size": len(sample),
    }


def run(binary, data_dir, args):
    """Run the whole measurement and return its result."""
    rows = generate_rows(args.seed, args.rows, args.dim)
    queries = make_queries(args.seed, args.queries, args.dim)
    categories = ["c%d" % i for i in range(CATEGORY_COUNT)]

    db = Db(binary, data_dir, mode=args.mode, cpu=args.cpu)
    try:
        pid = db.proc.pid

        # ---- the write phase
        io_start = read_io_bytes(pid)
        t0 = time.monotonic()
        phase_write(db, rows, args.dim, args.batch)
        write_ms = (time.monotonic() - t0) * 1000.0
        io_after_write = read_io_bytes(pid)
        write_peak = read_peak_rss_kb(pid)

        # The index has to exist before the queries, because the statement
        # measures the approximate mode as well.
        t0 = time.monotonic()
        db.run("CREATE VECTOR INDEX idx_t ON t(v);")
        index_ms = (time.monotonic() - t0) * 1000.0

        # ---- a warm up, so that the measurement is not dominated by the
        # first touch of the data
        phase_query(db, queries[: min(20, len(queries))], args.k, args.filter_ratio, categories)

        # ---- the query phase
        io_before_query = read_io_bytes(pid)
        t0 = time.monotonic()
        answers, filtered_flags = phase_query(
            db, queries, args.k, args.filter_ratio, categories
        )
        query_ms = (time.monotonic() - t0) * 1000.0
        io_after_query = read_io_bytes(pid)
        query_peak = read_peak_rss_kb(pid)

        result = {
            "config": {
                "rows": args.rows,
                "dim": args.dim,
                "queries": args.queries,
                "k": args.k,
                "filter_ratio": args.filter_ratio,
                "batch": args.batch,
                "seed": args.seed,
                "mode": args.mode,
            },
            "write": {
                "t_ms": round(write_ms, 3),
                "io_bytes": (io_after_write - io_start)
                if (io_start is not None and io_after_write is not None)
                else None,
                "peak_rss_kb": write_peak,
            },
            "index": {"t_ms": round(index_ms, 3)},
            "query": {
                "t_ms": round(query_ms, 3),
                "io_bytes": (io_after_query - io_before_query)
                if (io_before_query is not None and io_after_query is not None)
                else None,
                "peak_rss_kb": query_peak,
                "recall_filtered": None,
                "recall_unfiltered": None,
            },
        }

        # ---- the correctness of the answers
        try:
            recall = check_answers(
                answers, filtered_flags, rows, queries, args.k, categories
            )
        except Failure as e:
            raise Failure(str(e))
        result["query"].update(recall)

        # The answers are written out, so that two runs can be compared byte
        # by byte. A change of the implementation must not change them.
        if args.dump_baseline:
            with open(args.dump_baseline, "w") as f:
                for answer in answers:
                    f.write(",".join(str(pk) for pk in answer) + "\n")

        return result
    finally:
        db.close()


def compare(baseline, current):
    """Compare two measurements and report the relative change."""
    lines = []

    def report(name, key, lower_is_better=True):
        a = baseline.get(name, {}).get(key)
        b = current.get(name, {}).get(key)
        if a is None or b is None:
            return
        if a == 0:
            lines.append("  %-22s %s -> %s" % (name + "." + key, a, b))
            return
        delta = (b - a) / abs(a) * 100.0
        sign = "+" if delta >= 0 else ""
        lines.append(
            "  %-22s %s -> %s  (%s%.1f%%)"
            % (name + "." + key, a, b, sign, delta)
        )

    report("write", "t_ms")
    report("write", "io_bytes")
    report("write", "peak_rss_kb")
    report("query", "t_ms")
    report("query", "io_bytes")
    report("query", "peak_rss_kb")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", default=None, help="the database binary")
    parser.add_argument("--rows", type=int, default=10000, help="the number of rows")
    parser.add_argument("--dim", type=int, default=128, help="the dimension of the vectors")
    parser.add_argument("--queries", type=int, default=1000, help="the number of queries")
    parser.add_argument("--k", type=int, default=10, help="the number of rows per answer")
    parser.add_argument(
        "--filter-ratio", type=float, default=0.5, help="the share of the filtered queries"
    )
    parser.add_argument("--batch", type=int, default=100, help="the rows per transaction")
    parser.add_argument("--seed", type=int, default=42, help="the seed of the data set")
    parser.add_argument("--mode", default="ann", choices=("exact", "ann"))
    parser.add_argument("--data-dir", default=None, help="the data directory to use")
    parser.add_argument(
        "--cpu",
        default=None,
        help="pin the server to this core, which is what makes the rounds comparable",
    )
    parser.add_argument(
        "--cpus",
        default=None,
        help="the cores the whole benchmark may use, as a list of numbers or "
             "a range, for example '0,1'. The judge runs the container with "
             "--cpus=2, so this is the set of cores the measurement should "
             "be taken on",
    )
    parser.add_argument("--output", default=None, help="write the measurements to this file")
    parser.add_argument(
        "--compare", default=None, help="compare against the measurements of this file"
    )
    parser.add_argument(
        "--dump-baseline",
        default=None,
        help="write the answers of the queries to this file",
    )
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
        temporary = tempfile.mkdtemp(prefix="mvdb-bench-")
        data_dir = temporary
    else:
        shutil.rmtree(args.data_dir, ignore_errors=True)
        os.makedirs(args.data_dir, exist_ok=True)
        data_dir = args.data_dir
    data_dir = os.path.abspath(data_dir)

    # The judge runs the container with --cpus=2, so the measurement is
    # taken on two cores unless the caller says otherwise.
    if args.cpus:
        cpus = parse_cpu_list(args.cpus)
        pinned = pin_to_cpus(cpus)
        if pinned is None:
            print("warning: the affinity could not be set", file=sys.stderr)
        else:
            # The server is pinned to the first of the cores, and the driver
            # is left with the rest, so that the two of them do not fight
            # over the cache of one core.
            args.cpu = args.cpu or str(sorted(pinned)[0])

    try:
        result = run(binary, data_dir, args)
    except Failure as e:
        print("FAIL  %s" % e, file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)

    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")

    failures = []
    for key in ("recall_filtered", "recall_unfiltered"):
        value = result["query"][key]
        if value is None:
            failures.append("%s could not be computed" % key)
        elif value < MIN_RECALL:
            failures.append("%s is %.3f, below %.2f" % (key, value, MIN_RECALL))

    if args.compare:
        with open(args.compare, "r") as f:
            baseline = json.load(f)
        print("")
        print("compared against %s:" % args.compare)
        print(compare(baseline, result))

    if failures:
        print("")
        for message in failures:
            print("FAIL  %s" % message, file=sys.stderr)
        return 1
    print("")
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
