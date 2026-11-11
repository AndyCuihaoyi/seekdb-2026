#!/usr/bin/env python3
"""Profile the query phase of the benchmark, with the driver and the server
pinned to the same core, so that the samples attribute the compute and the
machine's noise does not.

The release profile of the crate sets `strip = true`, so the binary has no
symbols and `perf` reports raw addresses. Build without the stripping for
the profile, and with it for the delivery.

Usage: python3 tools/perf_query_only.py <binary> <mode> <out_dir> <perf-out> [cpu]
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bench as B  # noqa: E402


def main():
    binary = os.path.abspath(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    workdir = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="perfq")
    perf_out = sys.argv[4] if len(sys.argv) > 4 else "/tmp/perf.data"
    cpu = sys.argv[5] if len(sys.argv) > 5 else None

    rows = B.generate_rows(42, 10000, 128)
    queries = B.make_queries(7, 1000, 128)

    db = B.Db(binary, workdir, mode=mode, cpu=int(cpu) if cpu else None)
    B.phase_write(db, rows, 128, 100)
    db.run("CREATE VECTOR INDEX idx_t ON t(v);")

    warm = [B.vector_literal(q) for q in queries]
    cats = ["c%d" % i for i in range(B.CATEGORY_COUNT)]

    def sql_of(i, filtered):
        clause = " WHERE cat = 'c%d'" % (i % 8) if filtered else ""
        return (
            "SELECT id, L2_DISTANCE(v,'%s') AS distance FROM t%s "
            "ORDER BY distance LIMIT 10;" % (warm[i], clause)
        )

    # Warm up: get every block into the cache, so that the samples attribute
    # the compute and not the file reads.
    for i in range(1000):
        db.run(sql_of(i, i % 2 == 0))

    command = ["perf", "record", "-F", "999", "-g", "--call-graph=dwarf,4096",
               "-o", perf_out, "--"]
    if cpu is not None:
        command = ["taskset", "-c", str(cpu)] + command
    command += [binary, "--data-dir", workdir]

    lines = []
    for i in range(1000):
        lines.append(
            '{"id": %d, "op": "execute", "sql": "%s", "search_mode": "%s"}'
            % (i, sql_escape(sql_of(i, i % 2 == 0)), mode)
        )
    lines.append('{"id": 99999, "op": "shutdown"}')

    env = dict(os.environ)
    env["MINIVECDB_DATA_DIR"] = workdir
    env["MINIVECDB_MODE"] = mode
    proc = subprocess.run(command, input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    sys.stderr.write(proc.stderr[-3000:])
    print("perf data written to", perf_out)

    db.close()
    shutil.rmtree(workdir, ignore_errors=True)


def sql_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


if __name__ == "__main__":
    main()
