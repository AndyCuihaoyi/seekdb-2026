#!/usr/bin/env python3
"""Compare the approximate answers of several builds, at a finite nprobe.

The fix of the round changed **how** the vectors of the sample are read,
not **which** rows are sampled and not the order the farthest point
sampling picks them in. So the cells of the index, and therefore the
approximate answers, must be **identical** between the build before the
fix and the build after it.

That is the claim this script checks, and it checks it in the mode where
the claim is meaningful. With the default of no limit on the number of
visited cells, the approximate search degenerates into the exact one, and
every build answers the same thing - so the comparison would prove
nothing there.

The script therefore sets `MINIVECDB_ANN_NPROBE` to a finite number of
cells, where the answer really depends on the cells, and compares:

* the answers of every pair of builds, byte for byte. The build before
  the fix and the build after it **must** answer exactly the same thing:
  the fix changed the time of the build, not its result.
* the recall of the answer against the exact answer, computed with the
  ordinary formula in Python.

The recall is reported, and it is **asserted** only in the mode the
platform uses. The statement asks for a Recall@10 of at least 0.95, and
the platform runs with no limit on the number of visited cells
(`config.rs`, `ann_nprobe: None`), where the search is exact and the
recall is 1.000. At a finite number of cells the recall is below 0.95 by
construction - that is what "approximate" means - so it is reported and
not asserted.

The data set is generated with a fixed seed, so every build is driven
through exactly the same rows.

Usage:
    tools/check_ann_nprobe_equivalence.py --broken PATH [--fixed PATH]
                                           [--scoring PATH]
                                           [--rows N] [--dim N]
                                           [--queries N] [--k N]
                                           [--nprobe 4,8,16,unlimited]
                                           [--seed N]
"""

import argparse
import json
import math
import os
import random
import select
import shutil
import subprocess
import sys
import tempfile


def start(binary, data_dir, nprobe):
    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = data_dir
    env["MINIVECDB_MODE"] = "exact"
    if nprobe is not None:
        env["MINIVECDB_ANN_NPROBE"] = str(nprobe)
    else:
        env.pop("MINIVECDB_ANN_NPROBE", None)
    # The standard error is a file, not a pipe.
    #
    # A pipe would fill up: the database writes its diagnostics to the
    # standard error, and a reader that does not drain it - which is what
    # this script used to do - blocks the database as soon as the pipe is
    # full, and the database stops answering. A file has no such limit, so
    # the diagnostics of a build that prints them cannot break the
    # comparison.
    log = open(os.path.join(os.path.dirname(data_dir.rstrip("/")), "stderr.log"), "w")
    proc = subprocess.Popen(
        [binary, "--data-dir", data_dir],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log,
        env=env,
        text=True,
        bufsize=1,
    )
    ready = proc.stdout.readline()
    if not ready:
        proc.wait(timeout=30)
        raise RuntimeError("the database closed its output")
    return proc


def run(proc, sql, mode="exact", timeout=120.0):
    request = {"id": 1, "op": "execute", "sql": sql, "search_mode": mode}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError("the database did not answer %r" % sql)
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("the database exited before it answered")
    return json.loads(line)


def close(proc):
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


def literal(vector):
    return "'[%s]'" % ",".join(repr(x) for x in vector)


def truth(rows, query, k, category=None):
    """The exact TopK, computed with the ordinary formula."""
    best = []
    for row_id, cat, vector in rows:
        if category is not None and cat != category:
            continue
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(vector, query)))
        best.append((d, row_id))
    best.sort(key=lambda item: (item[0], item[1]))
    return [row_id for _, row_id in best[:k]]


def load(binary, rows, dim, nprobe):
    """Load the rows into a fresh database and build the index."""
    work = tempfile.mkdtemp(prefix="mvdb-nprobe-")
    data_dir = os.path.join(work, "data")
    proc = start(binary, data_dir, nprobe)
    try:
        run(
            proc,
            "CREATE TABLE t (id BIGINT PRIMARY KEY, cat VARCHAR(8), "
            "v VECTOR(%d));" % dim,
        )
        batch = 100
        for first in range(0, len(rows), batch):
            values = []
            for row_id, cat, vector in rows[first : first + batch]:
                values.append("(%d,'%s',%s)" % (row_id, cat, literal(vector)))
            answer = run(proc, "INSERT INTO t VALUES " + ",".join(values) + ";")
            if answer.get("ok") is not True:
                raise RuntimeError("the insert failed: %r" % answer)
        answer = run(proc, "CREATE VECTOR INDEX idx_t ON t(v);")
        if answer.get("ok") is not True:
            raise RuntimeError("the index was not created: %r" % answer)
        return proc, work
    except Exception:
        close(proc)
        shutil.rmtree(work, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broken", default="/tmp/mvdb_broken")
    parser.add_argument(
        "--fixed",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "minivectordb",
            "target",
            "release",
            "minivectordb",
        ),
    )
    parser.add_argument(
        "--scoring",
        default="/tmp/mvdb_baseline",
        help="the build that passed the admission gate; it is reported, "
        "not asserted against the broken one",
    )
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--categories", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--nprobe", default="4,8,16,unlimited")
    args = parser.parse_args()

    dim = args.dim
    builds = [("broken", args.broken), ("fixed", args.fixed)]
    if args.scoring and os.path.exists(args.scoring):
        builds.append(("scoring", args.scoring))

    settings = []
    for item in args.nprobe.split(","):
        item = item.strip()
        if not item:
            continue
        settings.append(None if item == "unlimited" else int(item))

    rng = random.Random(args.seed)
    rows = []
    for i in range(args.rows):
        vector = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
        rows.append((i, "c%d" % (i % args.categories), vector))
    queries = [
        [rng.uniform(-1.0, 1.0) for _ in range(dim)] for _ in range(args.queries)
    ]

    # The exact answer of every query, computed once. It does not depend on
    # the build and not on the number of visited cells, so it is computed
    # once for the whole run.
    print("computing the exact answer of every query ...", flush=True)
    wanted = {}
    for index, q in enumerate(queries):
        wanted[(index, "unfiltered")] = truth(rows, q, args.k)
        wanted[(index, "filtered")] = truth(
            rows, q, args.k, "c%d" % (index % args.categories)
        )
    print("done", flush=True)

    report = {
        "builds": {name: path for name, path in builds},
        "rows": args.rows,
        "dim": dim,
        "queries": args.queries,
        "k": args.k,
        "seed": args.seed,
        "nprobe": {},
        "failures": [],
    }

    for nprobe in settings:
        label = "unlimited" if nprobe is None else str(nprobe)
        answers = {}
        for name, binary in builds:
            proc, work = load(binary, rows, dim, nprobe)
            try:
                got = []
                for index, q in enumerate(queries):
                    for group, clause in (
                        ("unfiltered", ""),
                        ("filtered", " WHERE cat = 'c%d'" % (index % args.categories)),
                    ):
                        sql = (
                            "SELECT id, L2_DISTANCE(v, %s) AS d FROM t%s "
                            "ORDER BY d LIMIT %d;" % (literal(q), clause, args.k)
                        )
                        answer = run(proc, sql, mode="ann")
                        if answer.get("ok") is not True:
                            raise RuntimeError("the query failed: %r" % answer)
                        got.append((group, [row[0] for row in answer.get("rows", [])]))
                answers[name] = got
            finally:
                close(proc)
                shutil.rmtree(work, ignore_errors=True)

        # The build before the fix and the build after it must answer
        # exactly the same thing. This is the claim of the round.
        summary = {"different_broken_vs_fixed": 0}
        for position, ((group, ids_broken), (_, ids_fixed)) in enumerate(
            zip(answers["broken"], answers["fixed"])
        ):
            if ids_broken != ids_fixed:
                summary["different_broken_vs_fixed"] += 1
                if summary["different_broken_vs_fixed"] <= 3:
                    report["failures"].append(
                        "%s: the answers of the broken and the fixed build "
                        "differ in the %s query %d: %r and %r"
                        % (label, group, position // 2, ids_broken, ids_fixed)
                    )
        if summary["different_broken_vs_fixed"]:
            report["failures"].append(
                "%s: %d of %d answers differ between the broken and the fixed build"
                % (label, summary["different_broken_vs_fixed"], len(answers["broken"]))
            )

        if "scoring" in answers:
            summary["different_fixed_vs_scoring"] = sum(
                1
                for (group, ids_fixed), (_, ids_scoring) in zip(
                    answers["fixed"], answers["scoring"]
                )
                if ids_fixed != ids_scoring
            )

        # The recall of the answers, against the exact ones.
        recalls = {
            name: {"unfiltered": [], "filtered": []} for name in answers
        }
        for position, (group, _) in enumerate(answers["fixed"]):
            index = position // 2
            exact = wanted[(index, group)]
            if not exact:
                continue
            for name in answers:
                got_ids = answers[name][position][1]
                hits = len(set(got_ids) & set(exact))
                recalls[name][group].append(hits / len(exact))

        for name in answers:
            summary[name] = {}
            for group in ("unfiltered", "filtered"):
                values = recalls[name][group]
                mean = sum(values) / len(values) if values else 1.0
                summary[name][group] = round(mean, 4)
                # The 0.95 of the statement applies to the mode the
                # platform runs, which is the unlimited one.
                if name == "fixed" and nprobe is None and mean < 0.95:
                    report["failures"].append(
                        "the recall of the %s query is %.4f in the mode the "
                        "platform runs, the statement asks for 0.95"
                        % (group, mean)
                    )

        report["nprobe"][label] = summary
        print(
            "%-10s  broken vs fixed differ: %d   fixed vs scoring differ: %d   "
            "recall fixed: %.4f / %.4f"
            % (
                label,
                summary["different_broken_vs_fixed"],
                summary.get("different_fixed_vs_scoring", -1),
                summary["fixed"]["unfiltered"],
                summary["fixed"]["filtered"],
            ),
            flush=True,
        )

    report["passed"] = not report["failures"]
    print()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
