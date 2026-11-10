#!/usr/bin/env python3
"""A small client for the JSON Lines protocol of MiniVectorDB.

The competition protocol is JSON Lines: the server announces itself with
`{"type":"ready"}` and then answers one JSON object per request. The local
scripts use this module so that they all drive the database the same way the
judge does.

The module has no dependency outside the standard library.
"""

import json
import os
import subprocess
import time


class ProtocolError(Exception):
    """The database did not answer the way the protocol demands."""


class Database:
    """A running instance of the database under test.

    The client keeps one request open at a time: it writes a request, reads
    exactly one response line and checks that the response carries the id of
    the request. That is exactly what the judge does, so a bug in the
    protocol shows up here rather than in the evaluation.
    """

    def __init__(self, binary, data_dir, mode="exact", env=None, timeout=60.0):
        self.binary = binary
        self.timeout = timeout
        environment = dict(os.environ)
        environment["MINIVECDB_DATA_DIR"] = str(data_dir)
        environment["MINIVECDB_MODE"] = mode
        if env:
            environment.update(env)
        self.proc = subprocess.Popen(
            [binary, "--data-dir", str(data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            bufsize=1,
        )
        self.sequence = 0
        # The handshake has to arrive before the first request is sent.
        ready = self.proc.stdout.readline()
        if not ready:
            raise ProtocolError("the database closed its output before the handshake")
        try:
            message = json.loads(ready)
        except ValueError as exc:
            raise ProtocolError("the handshake is not JSON: %r (%s)" % (ready, exc))
        if message != {"type": "ready"}:
            raise ProtocolError("expected a ready message, got %r" % (message,))

    def run(self, sql, mode="exact"):
        """Run one statement and return its answer."""
        self.sequence += 1
        request = {
            "id": self.sequence,
            "op": "execute",
            "sql": sql.rstrip().rstrip(";"),
            "search_mode": mode,
        }
        return self._exchange(request)

    def _exchange(self, request):
        self.proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        raw = self.proc.stdout.readline()
        if not raw:
            raise ProtocolError(
                "the database closed its output while running %r"
                % request.get("sql", "")[:120]
            )
        try:
            answer = json.loads(raw)
        except ValueError as exc:
            raise ProtocolError("the answer is not JSON: %r (%s)" % (raw, exc))
        if answer.get("id") != request["id"]:
            raise ProtocolError(
                "the answer carries the id %r, expected %r"
                % (answer.get("id"), request["id"])
            )
        if not isinstance(answer.get("ok"), bool):
            raise ProtocolError("the answer has no boolean ok: %r" % (answer,))
        return answer

    def shutdown(self):
        """Ask the database to shut down and wait for the process to end."""
        try:
            self.sequence += 1
            self._exchange({"id": self.sequence, "op": "shutdown"})
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=self.timeout)
        except Exception:
            self.proc.kill()

    def close(self):
        """Stop the process, without asking it to shut down."""
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=self.timeout)
        except Exception:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def run_script(binary, data_dir, statements, mode="exact"):
    """Run a list of statements and return the list of their answers.

    The convenience wrapper is used by the scripts that only need the
    answers and not the interactive protocol.
    """
    answers = []
    with Database(binary, data_dir, mode=mode) as db:
        for sql in statements:
            answers.append(db.run(sql, mode=mode))
    return answers


def wait_for(predicate, timeout=10.0, interval=0.05):
    """Wait until `predicate` returns a true value, and return it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def main(argv=None):
    """Run a list of statements and print the answers.

    The script is the driver of the smoke test and of the ad hoc checks: it
    takes the statements on the command line and prints one JSON object per
    answer, so that a shell script can grep them.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, help="the database binary")
    parser.add_argument("--data-dir", required=True, help="the data directory")
    parser.add_argument("--mode", default="exact", choices=("exact", "ann"))
    parser.add_argument(
        "statements",
        nargs="*",
        help="the statements to run; put them after a '--' separator",
    )
    args = parser.parse_args(argv)

    answers = []
    db = Database(args.binary, args.data_dir, mode=args.mode)
    try:
        for sql in args.statements:
            answers.append(db.run(sql, mode=args.mode))
    finally:
        db.close()
    for answer in answers:
        print(json.dumps(answer, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
