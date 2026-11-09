#!/usr/bin/env bash
#
# Smoke tests for MiniVectorDB.
#
# The script covers the semantics that the published test package does not
# exercise on its own, but that the statement of the competition requires:
#
#   S3  CREATE TABLE IF NOT EXISTS is idempotent: the second run succeeds and
#       leaves the table exactly as it was.
#   S4  An explicit transaction that is still open when the input ends is
#       rolled back: its rows are not visible after a restart, while the rows
#       that were committed are.
#
# Both checks run in the exact mode and in the approximate mode, because the
# two modes follow different code paths.
#
# Usage:
#   tools/smoke.sh [--binary PATH]
#
# The script exits with a non-zero status when a check fails.

set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BINARY="${ROOT}/minivectordb/target/release/minivectordb"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mvdb-smoke-XXXXXX")"

cleanup() {
    rm -rf "${WORK}"
}
trap cleanup EXIT

while [ $# -gt 0 ]; do
    case "$1" in
        --binary)
            BINARY="$2"
            shift 2
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [ ! -x "${BINARY}" ]; then
    echo "smoke: binary not found: ${BINARY}" >&2
    echo "smoke: build it with: cargo build --release --manifest-path minivectordb/Cargo.toml" >&2
    exit 2
fi

FAILED=0

fail() {
    echo "FAIL  $1" >&2
    FAILED=1
}

pass() {
    echo "ok    $1"
}

# Run the statements given on stdin against a fresh data directory and print
# the output.
run_db() {
    local dir="$1"
    local mode="$2"
    shift 2
    printf '%s\n' "$@" | "${BINARY}" --data-dir "${dir}" --mode "${mode}" --output-format jsonl 2>/dev/null
}

# ---------------------------------------------------------------- S3

# The idempotent create table: the second statement succeeds and the table
# keeps its original shape.
check_idempotent_create() {
    local mode="$1"
    local dir="${WORK}/idempotent-${mode}"
    rm -rf "${dir}"
    mkdir -p "${dir}"

    local out
    out="$(run_db "${dir}" "${mode}" \
        "CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));" \
        "INSERT INTO t VALUES (1, '[1,2]');" \
        "CREATE TABLE IF NOT EXISTS t (id BIGINT PRIMARY KEY, v VECTOR(2));" \
        "SELECT id FROM t ORDER BY id;" \
        "CREATE TABLE IF NOT EXISTS other (id BIGINT PRIMARY KEY);")"

    if ! grep -q '"affected_rows":1' <<<"${out}"; then
        fail "S3/${mode}: the insert did not report one affected row"
        return
    fi
    if grep -qi 'error' <<<"${out}"; then
        fail "S3/${mode}: an error was reported: ${out}"
        return
    fi
    # The table that already existed still holds its row, and it still has
    # the column it was created with.
    if ! grep -q '"rows":\[\[1\]\]' <<<"${out}"; then
        fail "S3/${mode}: the existing table lost its row: ${out}"
        return
    fi
    # The table that did not exist yet was created.
    if ! grep -q '"affected_rows":0' <<<"${out}"; then
        fail "S3/${mode}: the new table was not created: ${out}"
        return
    fi
    pass "S3/${mode}: CREATE TABLE IF NOT EXISTS is idempotent"
}

# ---------------------------------------------------------------- S4

# An open transaction is rolled back when the input ends.
check_eof_rollback() {
    local mode="$1"
    local dir="${WORK}/eof-${mode}"
    rm -rf "${dir}"
    mkdir -p "${dir}"

    # The first session commits one row and leaves one transaction open.
    printf '%s\n' \
        "CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(2));" \
        "INSERT INTO t VALUES (1, '[1,2]');" \
        "BEGIN;" \
        "INSERT INTO t VALUES (2, '[3,4]');" \
        "UPDATE t SET v = '[9,9]' WHERE id = 1;" \
        | "${BINARY}" --data-dir "${dir}" --mode "${mode}" --output-format jsonl >/dev/null 2>&1

    # The second session reads the table: the committed row is there, the
    # staged one is not, and the update never happened.
    local out
    out="$(run_db "${dir}" "${mode}" "SELECT id, v FROM t ORDER BY id;")"

    if ! grep -q '"rows":\[\[1,\[1\.000000,2\.000000\]\]\]' <<<"${out}"; then
        fail "S4/${mode}: the committed row is not intact: ${out}"
        return
    fi
    if grep -q '3\.000000,4\.000000' <<<"${out}"; then
        fail "S4/${mode}: the rolled back row is still visible: ${out}"
        return
    fi
    if grep -q '9\.000000,9\.000000' <<<"${out}"; then
        fail "S4/${mode}: the rolled back update is still visible: ${out}"
        return
    fi
    pass "S4/${mode}: an open transaction is rolled back at the end of the input"
}

for mode in exact ann; do
    check_idempotent_create "${mode}"
    check_eof_rollback "${mode}"
done

if [ "${FAILED}" -ne 0 ]; then
    echo "smoke: some checks failed" >&2
    exit 1
fi
echo "smoke: all checks passed"
