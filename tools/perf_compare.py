#!/usr/bin/env python3
"""Compare the performance of two builds of MiniVectorDB.

The comparison follows the measurement of the statement: the data set, the
query load and the three axes are the ones of `tools/bench.py`. Each build is
measured several times and the median of the runs is reported, because a
single run is noisy enough to hide a small change.

The script measures three rounds per build and per mode, and writes the
result as JSON so that the numbers can be compared with the ones of another
run.

Usage:
    tools/perf_compare.py --before BIN_A --after BIN_B [--rounds 3]
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def measure(binary, args, mode):
    """Measure one build once and return the result of the benchmark."""
    with tempfile.TemporaryDirectory(prefix="mvdb-perf-") as data_dir:
        command = [
            sys.executable,
            os.path.join(HERE, "bench.py"),
            "--binary",
            binary,
            "--rows",
            str(args.rows),
            "--dim",
            str(args.dim),
            "--queries",
            str(args.queries),
            "--k",
            str(args.k),
            "--filter-ratio",
            str(args.filter_ratio),
            "--seed",
            str(args.seed),
            "--mode",
            mode,
            "--data-dir",
            os.path.join(data_dir, "data"),
        ]
        out = subprocess.run(command, capture_output=True, text=True)
        if out.returncode != 0:
            raise SystemExit(
                "the benchmark of %s failed:\n%s" % (binary, out.stderr or out.stdout)
            )
        return parse_json(out.stdout)


def parse_json(text):
    """Read the JSON object out of the output of the benchmark.

    The benchmark prints the measurements and then a short summary, so the
    JSON is only the first block of the output.
    """
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text.lstrip())
    return obj


def median_of(values):
    return statistics.median(values)


def summarise(runs):
    """Reduce the runs of one build to the median of every axis."""
    query = [r["query"] for r in runs]
    write = [r["write"] for r in runs]
    return {
        "query_t_ms": median_of([q["t_ms"] for q in query]),
        "query_io_bytes": median_of([q["io_bytes"] for q in query]),
        "query_peak_rss_kb": median_of([q["peak_rss_kb"] for q in query]),
        "write_t_ms": median_of([w["t_ms"] for w in write]),
        "write_io_bytes": median_of([w["io_bytes"] for w in write]),
        "write_peak_rss_kb": median_of([w["peak_rss_kb"] for w in write]),
        "recall_filtered": query[0]["recall_filtered"],
        "recall_unfiltered": query[0]["recall_unfiltered"],
        "runs": len(runs),
    }


def ratio(before, after, key):
    """The factor by which a value changed, as a readable string."""
    old = before[key]
    new = after[key]
    if old == 0:
        return "n/a"
    factor = old / new if new else float("inf")
    return "%.2fx" % factor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="the binary of the old build")
    parser.add_argument("--after", required=True, help="the binary of the new build")
    parser.add_argument("--rounds", type=int, default=3, help="the runs per build")
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--filter-ratio", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default=None, help="write the comparison as JSON")
    args = parser.parse_args()

    report = {"config": vars(args), "modes": {}}
    for mode in ("exact", "ann"):
        before_runs = [measure(args.before, args, mode) for _ in range(args.rounds)]
        after_runs = [measure(args.after, args, mode) for _ in range(args.rounds)]
        before = summarise(before_runs)
        after = summarise(after_runs)
        report["modes"][mode] = {"before": before, "after": after}
        print("== %s ==" % mode)
        for key in (
            "query_t_ms",
            "query_io_bytes",
            "query_peak_rss_kb",
            "write_t_ms",
            "write_io_bytes",
            "write_peak_rss_kb",
        ):
            print(
                "  %-22s %14.1f -> %14.1f  (%s)"
                % (key, before[key], after[key], ratio(before, after, key))
            )
        print(
            "  recall filtered %s, unfiltered %s"
            % (after["recall_filtered"], after["recall_unfiltered"])
        )

    if args.output:
        with open(args.output, "w") as f:
            f.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
