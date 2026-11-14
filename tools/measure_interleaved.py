#!/usr/bin/env python3
"""Compare two builds of the database, **interleaved**, on the same shape.

A single run of the write is not a measurement: at four million rows the
noise of the machine is about forty seconds, and a difference of seven
seconds is invisible in it. Two runs of the same binary, one after the
other, have differed by more than a hundred seconds.

The way out is to **interleave**: the two builds are measured one after
the other, again and again, and the medians are compared. The slow
drift of the machine then falls on both builds alike, and what is left is
the difference between them.

Usage:
    tools/measure_interleaved.py --before PATH --after PATH [--rounds N]
                                  [--rows N] [--text T] [--dim D]
                                  [--batch N] [--output FILE]
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
BUDGET = os.path.join(HERE, "measure_case_budget.py")


def one_round(binary, rows, text, dim, batch):
    """One measurement of the whole case, and the phases that matter."""
    out = tempfile.mktemp(prefix="mvdb-inter-", suffix=".json")
    try:
        subprocess.run(
            [sys.executable, BUDGET, "--binary", binary,
             "--rows", str(rows), "--text", str(text), "--dim", str(dim),
             "--batch", str(batch), "--output", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        with open(out, "r", encoding="utf-8") as handle:
            load = json.load(handle)["loads"][0]
        return {
            "write_s": load["write_s"],
            "restart_handshake_s": load["restart_handshake_s"],
            "case_total_s": load["case_total_s"],
        }
    finally:
        if os.path.exists(out):
            os.remove(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--rows", type=int, default=1000000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--batch", type=int, default=100)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    report = {"args": vars(args), "rounds": []}
    for round_index in range(args.rounds):
        before = one_round(args.before, args.rows, args.text, args.dim, args.batch)
        after = one_round(args.after, args.rows, args.text, args.dim, args.batch)
        report["rounds"].append({"round": round_index + 1,
                                 "before": before, "after": after})
        print("round %d: before write %7.2f s, after write %7.2f s, "
              "the difference %+7.2f s"
              % (round_index + 1, before["write_s"], after["write_s"],
                 after["write_s"] - before["write_s"]))

    for phase in ("write_s", "restart_handshake_s", "case_total_s"):
        b = [r["before"][phase] for r in report["rounds"]]
        a = [r["after"][phase] for r in report["rounds"]]
        report[phase] = {
            "before_median": round(statistics.median(b), 3),
            "after_median": round(statistics.median(a), 3),
            "before_all": b,
            "after_all": a,
            "difference": round(statistics.median(a) - statistics.median(b), 3),
            "spread": round(max(b + a) - min(b + a), 3),
        }
        print("%-24s before %8.2f s, after %8.2f s, the difference %+7.2f s "
              "(the spread of all the runs: %.2f s)"
              % (phase, statistics.median(b), statistics.median(a),
                 statistics.median(a) - statistics.median(b),
                 max(b + a) - min(b + a)))

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
