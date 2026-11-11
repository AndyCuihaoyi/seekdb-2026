"""Split the measured input and output of the write phase.

The benchmark counts the bytes read and the bytes written together, so a
change that only moves the data between the log and the data files is
invisible. This script shows the two separately.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench as B


def io_of(pid):
    out = {}
    try:
        with open("/proc/%d/io" % pid, "r") as f:
            for line in f:
                key, _, value = line.partition(":")
                out[key.strip()] = int(value.strip())
    except OSError:
        pass
    return out


def main():
    binary = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.abspath(
        "minivectordb/target/release/minivectordb")
    mode = sys.argv[2] if len(sys.argv) > 2 else "exact"
    nrows = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    batch = int(sys.argv[4]) if len(sys.argv) > 4 else 100

    workdir = tempfile.mkdtemp(prefix="wioprobe")
    rows = B.generate_rows(42, nrows, 128)

    lines = ['{"id": 0, "op": "execute", "sql": "CREATE TABLE t (id BIGINT '
             'PRIMARY KEY, cat VARCHAR(8), v VECTOR(128));", "search_mode": "%s"}' % mode]
    for b in range(0, nrows, batch):
        chunk = rows[b:b + batch]
        values = ",".join(
            "(%d,'%s','%s')" % (pk, cat, B.vector_literal(vec))
            for pk, cat, vec in chunk
        )
        for stmt in ("BEGIN;", "INSERT INTO t VALUES " + values + ";", "COMMIT;"):
            lines.append(
                '{"id": 0, "op": "execute", "sql": "%s", "search_mode": "%s"}'
                % (stmt.replace('"', '\\"'), mode)
            )
    lines.append('{"id": 99999, "op": "shutdown"}')

    env = dict(os.environ)
    env.update(MINIVECDB_DATA_DIR=workdir, MINIVECDB_MODE=mode)
    proc = subprocess.Popen([binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    pid = proc.pid
    before = io_of(pid)

    import threading

    def feed():
        proc.stdin.write("\n".join(lines) + "\n")
        proc.stdin.flush()
        proc.stdin.close()

    feeder = threading.Thread(target=feed)
    feeder.start()
    # The counters are read while the process is still running, because
    # they are gone once it has exited.
    last = {}
    while proc.poll() is None:
        last = io_of(pid)
        time.sleep(0.01)
    feeder.join()
    proc.wait(timeout=120)
    after = last or io_of(pid)
    for key in ("rchar", "wchar", "syscr", "syscw", "read_bytes", "write_bytes"):
        if key in after:
            print("%-12s %12d" % (key, after[key] - before.get(key, 0)))
    for name in sorted(os.listdir(workdir)):
        path = os.path.join(workdir, name)
        if os.path.isfile(path):
            print("file %-12s %12d" % (name, os.path.getsize(path)))
        else:
            for sub in sorted(os.listdir(path)):
                p2 = os.path.join(path, sub)
                if os.path.isfile(p2):
                    print("file %-12s %12d" % (name + "/" + sub, os.path.getsize(p2)))


if __name__ == "__main__":
    main()
