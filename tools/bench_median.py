#!/usr/bin/env python3
"""Run the benchmark a few times and report the median of every measurement.

The platform takes the median of three rounds, so the local comparison has
to do the same to be meaningful.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=None)
    ap.add_argument("--mode", default="exact")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--queries", type=int, default=1000)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument(
        "--cpu",
        default=None,
        help="pin the server to this core; without it the rounds of this "
             "machine differ by up to 70% and the median means little",
    )
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    binary = args.binary or os.path.join(root, "minivectordb", "target", "release", "minivectordb")

    command = [sys.executable, os.path.join(root, "tools", "bench.py"),
               "--binary", binary, "--mode", args.mode,
               "--rows", str(args.rows), "--dim", str(args.dim),
               "--queries", str(args.queries)]
    if args.cpu is not None:
        command += ["--cpu", str(args.cpu)]

    runs = []
    for _ in range(args.rounds):
        out = subprocess.run(command, capture_output=True, text=True)
        if out.returncode != 0:
            sys.stderr.write(out.stdout[-3000:])
            sys.stderr.write(out.stderr[-3000:])
            return 1
        text = out.stdout
        start = text.index("{")
        end = text.rindex("}") + 1
        runs.append(json.loads(text[start:end]))

    def med(path):
        values = []
        for r in runs:
            node = r
            for key in path:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            if isinstance(node, dict):
                node = node.get("t_ms")
            if node is None:
                return None
            values.append(node)
        return statistics.median(values)

    def medb(path):
        values = []
        for r in runs:
            node = r
            for key in path:
                node = node.get(key, {}) if isinstance(node, dict) else {}
            if node is None:
                return None
            values.append(node)
        return statistics.median(values)

    summary = {
        "mode": args.mode,
        "rows": args.rows,
        "queries": args.queries,
        "rounds": args.rounds,
        "query_t_ms": med(["query", "t_ms"]),
        "query_io_bytes": medb(["query", "io_bytes"]),
        "query_peak_rss_kb": medb(["query", "peak_rss_kb"]),
        "write_t_ms": med(["write", "t_ms"]),
        "write_io_bytes": medb(["write", "io_bytes"]),
        "write_peak_rss_kb": medb(["write", "peak_rss_kb"]),
        "recall_filtered": medb(["query", "recall_filtered"]),
        "recall_unfiltered": medb(["query", "recall_unfiltered"]),
        "runs": runs,
    }
    print(json.dumps(summary, indent=2))
    if args.output:
        with open(args.output, "w") as f:
            json.dump(summary, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
