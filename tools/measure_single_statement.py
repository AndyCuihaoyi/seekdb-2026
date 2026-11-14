#!/usr/bin/env python3
"""Find the number of rows a **single** INSERT statement can write.

The judge charges every statement sixty seconds and gives the database
two gigabytes. A statement that writes its rows in **one** INSERT holds
all of them in memory at once: the staged rows of the transaction, the
log records of the statement, and the row images. So there is a number of
rows above which a single INSERT does not fit into the memory of the
judge, and the database dies with

    memory allocation of N bytes failed

which the judge reports as `database exited before response` - the
signature that is not a timeout.

This script finds that number, for a given text length, under the limits
of the judge, and it does it for two binaries so that a change can be
compared with the one before it.

Usage:
    tools/measure_single_statement.py --binary PATH [--compare PATH]
                                      [--rows N] [--text T] [--dim D]
                                      [--groups N] [--output FILE]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# The limits of the judge: two CPUs and two gigabytes of memory.
MEMORY_LIMIT_KB = 2 * 1024 * 1024


def build_command(binary, cpus):
    inner = (
        "ulimit -v %d 2>/dev/null || true; exec %s --data-dir {data_dir}"
        % (MEMORY_LIMIT_KB, binary)
    )
    if cpus:
        return ["taskset", "-c", "0,%d" % (cpus - 1), "sh", "-c", inner]
    return ["sh", "-c", inner]


def make_insert(rows, text, dim):
    """The one statement that writes `rows` rows."""
    body = "x" * text if text else ""
    values = []
    for row_id in range(rows):
        vec = ",".join(str((row_id * (i + 1)) % 100) for i in range(dim))
        if text:
            values.append(
                "(%d,'c%d','%s','[%s]')" % (row_id, row_id % 8, body, vec)
            )
        else:
            values.append("(%d,'c%d','[%s]')" % (row_id, row_id % 8, vec))
    return "INSERT INTO docs VALUES " + ",".join(values) + ";"


def run_one(binary, rows, text, dim, cpus):
    """Write `rows` rows with one statement, and say what happened."""
    work = tempfile.mkdtemp(prefix="mvdb-onestmt-")
    try:
        proc = subprocess.Popen(
            build_command(binary, cpus),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        ready = proc.stdout.readline()
        if '"ready"' not in ready:
            return {"verdict": "no handshake", "stderr": ready[:200]}

        def send(sql):
            proc.stdin.write(json.dumps({"id": 1, "op": "execute", "sql": sql}) + "\n")
            proc.stdin.flush()
            return proc.stdout.readline()

        send("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), "
             "content VARCHAR(%d), embedding VECTOR(%d));" % (max(text, 1), dim))

        # The statement is prepared before the clock starts, because the
        # judge reads the statements of a case from a file.
        statement = make_insert(rows, text, dim)
        started = time.monotonic()
        try:
            line = send(statement)
        except (BrokenPipeError, OSError) as exc:
            return {"verdict": "DIED: %s" % type(exc).__name__}
        took = time.monotonic() - started
        if not line:
            # The database is gone. Its last words are on the error stream.
            proc.kill()
            err = proc.stderr.read()
            return {
                "verdict": "DIED",
                "s": round(took, 3),
                "stderr": err.strip().splitlines()[-1] if err.strip() else "",
            }
        answer = json.loads(line)
        if answer.get("ok") is not True:
            return {"verdict": "REFUSED", "answer": str(answer)[:200]}
        return {
            "verdict": "OK",
            "s": round(took, 3),
            "rows": answer.get("affected_rows"),
            "rows_per_second": round(rows / took) if took else 0,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--compare", default=None,
                        help="the binary before the change, for the comparison")
    parser.add_argument("--rows", type=int, default=200000)
    parser.add_argument("--text", type=int, default=1000)
    parser.add_argument("--dim", type=int, default=128)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    report = {"args": vars(args), "runs": {}}
    for name, binary in (("binary", args.binary), ("compare", args.compare)):
        if not binary:
            continue
        out = run_one(binary, args.rows, args.text, args.dim, args.cpus)
        report["runs"][name] = out
        print("%-8s %7d rows in one statement -> %s  %s"
              % (name, args.rows, out["verdict"],
                 out.get("s", "") and ("%.3f s" % out["s"]) or ""))
        if out.get("stderr"):
            print("         the last words: %s" % out["stderr"])

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
