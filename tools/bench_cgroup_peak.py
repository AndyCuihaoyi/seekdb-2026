#!/usr/bin/env python3
"""Measure the peak memory of the database the way the platform measures it.

The statement of the competition says that the peak memory is read from the
`memory.peak` file of the cgroup of the container, and that it includes the
page cache of the files under `/data`. The resident set size of the server
process, which the local benchmark reports, does *not* include the page cache,
so the two numbers are not the same and the smaller one is the wrong one to
look at.

This script therefore runs the whole benchmark inside a cgroup of its own and
reads `memory.peak` and `memory.current` of that cgroup. When the machine
does not allow a cgroup to be created, the script falls back to the resident
set size and says so, so that the number in the report is never silently
taken from the wrong source.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CGROUP_V2 = "/sys/fs/cgroup"


def find_writable_cgroup():
    """Find a cgroup of the current process that can hold a child cgroup."""
    try:
        with open(os.path.join(CGROUP_V2, "cgroup.controllers")) as f:
            controllers = f.read().split()
    except OSError:
        return None, None
    if "memory" not in controllers:
        return None, None
    # The path of the current process inside the cgroup hierarchy.
    try:
        with open(os.path.join(CGROUP_V2, "cgroup.procs")) as f:
            pass
    except OSError:
        return None, None
    current = os.path.join(CGROUP_V2, "cgroup")
    try:
        with open(os.path.join(current, "cgroup.procs")) as f:
            procs = [line for line in f.read().split() if line]
    except OSError:
        return None, None
    return current, controllers


def read_cgroup_peak(cgroup_dir):
    """Read the peak memory of a cgroup, in bytes."""
    out = {}
    for name in ("memory.peak", "memory.current", "memory.max"):
        path = os.path.join(cgroup_dir, name)
        try:
            with open(path) as f:
                out[name.split(".")[1]] = f.read().strip()
        except OSError:
            out[name.split(".")[1]] = None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=None)
    ap.add_argument("--mode", default="exact")
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--queries", type=int, default=1000)
    ap.add_argument("--output", default=None)
    args = ap.parse_args()

    binary = args.binary or os.path.join(ROOT, "minivectordb", "target", "release", "minivectordb")
    work = tempfile.mkdtemp(prefix="mvdb-cgroup-")
    try:
        current, controllers = find_writable_cgroup()
        child = None
        if current is not None:
            child = os.path.join(current, "mvdb-peak-%d" % os.getpid())
            try:
                os.makedirs(child, exist_ok=True)
                for name in ("memory",):
                    with open(os.path.join(child, "cgroup.subtree_control"), "w") as f:
                        f.write("+" + name)
                with open(os.path.join(child, "cgroup.procs"), "w") as f:
                    f.write(str(os.getpid()))
            except OSError as e:
                sys.stderr.write("could not create a cgroup: %s\n" % e)
                child = None

        out = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "bench.py"),
             "--binary", binary, "--mode", args.mode,
             "--rows", str(args.rows), "--dim", str(args.dim),
             "--queries", str(args.queries),
             "--data-dir", os.path.join(work, "data")],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            sys.stderr.write(out.stdout[-3000:])
            sys.stderr.write(out.stderr[-3000:])
            return 1
        text = out.stdout
        start = text.index("{")
        end = text.rindex("}") + 1
        result = json.loads(text[start:end])

        report = {"mode": args.mode, "rows": args.rows, "queries": args.queries}
        report["bench"] = result
        if child is not None:
            report["cgroup"] = read_cgroup_peak(child)
            report["cgroup_path"] = child
        else:
            report["cgroup"] = None
            report["cgroup_note"] = (
                "no writable cgroup was available; the numbers are the resident "
                "set size of the server process, which does not include the "
                "page cache of the data directory"
            )

        summary = {
            "mode": args.mode,
            "query_t_ms": result["query"]["t_ms"],
            "query_io_bytes": result["query"]["io_bytes"],
            "query_peak_rss_kb": result["query"]["peak_rss_kb"],
            "write_t_ms": result["write"]["t_ms"],
            "write_io_bytes": result["write"]["io_bytes"],
            "write_peak_rss_kb": result["write"]["peak_rss_kb"],
            "recall_filtered": result["query"]["recall_filtered"],
            "recall_unfiltered": result["query"]["recall_unfiltered"],
        }
        if report["cgroup"]:
            peak = report["cgroup"].get("peak")
            if peak and peak.isdigit():
                summary["cgroup_peak_bytes"] = int(peak)
                summary["cgroup_peak_kb"] = int(peak) // 1024
        print(json.dumps(summary, indent=2, sort_keys=True))
        if args.output:
            with open(args.output, "w") as f:
                json.dump(report, f, indent=2, sort_keys=True)
                f.write("\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
