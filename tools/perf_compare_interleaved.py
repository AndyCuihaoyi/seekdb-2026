#!/usr/bin/env python3
"""Compare two builds with the order of the runs interleaved.

`tools/perf_compare.py` runs every round of the old build and then every
round of the new one. That makes the **second** set pay for the state the
first one leaves behind - the page cache of the kernel, the frequency of
the processor, the heat - and the difference it reports is the order, not
the build. The measurement of this round showed it: the exact axis, whose
code was **not touched at all**, came out at 0.75x in one order and at
1.21x in the other.

This tool interleaves the rounds: old, new, old, new, and so on. The
order of the two builds is then the same for both of them, and the
difference that is left is the build.

The tool reports the median of the rounds, and it reports the ratio of
the medians.

Usage:
    tools/perf_compare_interleaved.py --before PATH --after PATH
                                       [--rounds N] [--rows N] [--dim N]
                                       [--queries N] [--mode exact|ann]
"""

import argparse
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import perf_compare  # noqa: E402


def numeric_ratio(old, new):
    """The factor by which a value changed, as a number."""
    if not new:
        return float("inf")
    return old / new


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--filter-ratio", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mode", default="ann", choices=("exact", "ann"))
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    runs = {"before": [], "after": []}
    for round_index in range(args.rounds):
        # The order of the two builds is swapped every round, so that
        # neither of them is always the second one.
        order = ("before", "after") if round_index % 2 == 0 else ("after", "before")
        for name in order:
            binary = args.before if name == "before" else args.after
            runs[name].append(perf_compare.measure(binary, args, args.mode))

    report = {"rounds": args.rounds, "mode": args.mode}
    for name in ("before", "after"):
        summary = perf_compare.summarise(runs[name])
        report[name] = summary
        report[name]["query_t_ms_all"] = [round_["query"]["t_ms"] for round_ in runs[name]]
        report[name]["write_t_ms_all"] = [round_["write"]["t_ms"] for round_ in runs[name]]

    report["ratio"] = {
        "query_t_ms": numeric_ratio(report["before"]["query_t_ms"],
                                    report["after"]["query_t_ms"]),
        "write_t_ms": numeric_ratio(report["before"]["write_t_ms"],
                                    report["after"]["write_t_ms"]),
    }
    report["recall_filtered"] = report["after"]["recall_filtered"]
    report["recall_unfiltered"] = report["after"]["recall_unfiltered"]

    print("== %s, %d rounds, interleaved ==" % (args.mode, args.rounds))
    for key in ("query_t_ms", "write_t_ms"):
        print("  %-14s %10.1f -> %10.1f  (%.2fx)"
              % (key, report["before"][key], report["after"][key], report["ratio"][key]))
    print("  the query time of every round, before: %s"
          % ["%.1f" % v for v in report["before"]["query_t_ms_all"]])
    print("  the query time of every round, after:  %s"
          % ["%.1f" % v for v in report["after"]["query_t_ms_all"]])
    print("  recall filtered %s, unfiltered %s"
          % (report["recall_filtered"], report["recall_unfiltered"]))

    if args.output:
        with open(args.output, "w") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print("  written to %s" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
