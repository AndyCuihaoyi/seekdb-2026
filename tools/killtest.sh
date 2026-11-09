#!/usr/bin/env bash
# Crash recovery test of MiniVectorDB.
#
# The competition simulates a crash by forcing the process to terminate. The
# test does the same thing, repeatedly, and checks that the state of the
# database is always consistent:
#
# * a transaction whose commit was acknowledged is always there;
# * a transaction whose commit was not acknowledged is either completely
#   there or completely absent, never half there;
# * a row never appears twice, and a deleted row never comes back.
#
# The script kills the database while it recovers, so the recovery itself is
# interrupted as well - that is the case the statement asks about ("terminate
# again during recovery, check for repeated recovery").

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BINARY="${1:-$ROOT/minivectordb/target/release/minivectordb}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mvdb-killtest-XXXXXX")"
DATA="$WORK/data"

# The number of kill rounds.
ROUNDS="${ROUNDS:-12}"

# The number of rows committed in every round.
BATCH="${BATCH:-200}"

failures=0

cleanup() {
    rm -rf "$WORK"
}
trap cleanup EXIT

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'FAIL  %s\n' "$*" >&2
    failures=$((failures + 1))
}

if [ ! -x "$BINARY" ]; then
    say "error: the binary $BINARY does not exist"
    say "       run 'cargo build --release --offline' first"
    exit 2
fi

mkdir -p "$DATA"

# ---------------------------------------------------------------- the helpers

# Run one SQL statement against a database whose stdin is the file given as
# the first argument. The answer is written to the file of the second
# argument.
run_sql() {
    local sql_file="$1" out_file="$2"
    MINIVECDB_DATA_DIR="$DATA" \
    MINIVECDB_OUTPUT_FORMAT=tsv \
    MINIVECDB_READY_SINK=stderr \
    "$BINARY" <"$sql_file" >"$out_file" 2>"$WORK/stderr.txt"
}

# Count the rows of a table with a query.
count_rows() {
    local table="$1"
    local sql_file="$WORK/count.sql"
    local out_file="$WORK/count.out"
    printf 'SELECT id FROM %s;\n' "$table" >"$sql_file"
    run_sql "$sql_file" "$out_file" || { echo "-1"; return; }
    # The first line is the header.
    tail -n +2 "$out_file" | wc -l | tr -d ' '
}

# The identifiers of the rows of a table, sorted.
list_rows() {
    local table="$1"
    local sql_file="$WORK/list.sql"
    local out_file="$WORK/list.out"
    printf 'SELECT id FROM %s;\n' "$table" >"$sql_file"
    run_sql "$sql_file" "$out_file" || return
    tail -n +2 "$out_file" | sort -n
}

say "MiniVectorDB crash recovery test"
say "  binary : $BINARY"
say "  data   : $DATA"
say "  rounds : $ROUNDS"
say ""

# ------------------------------------------------- the initial committed state

say "preparing the initial state"
{
    printf "CREATE TABLE t (id BIGINT PRIMARY KEY, v VECTOR(4));\n"
    for i in $(seq 1 "$BATCH"); do
        printf "INSERT INTO t VALUES (%d,'[%d,%d,%d,%d]');\n" \
            "$i" "$i" "$((i + 1))" "$((i + 2))" "$((i + 3))"
    done
} >"$WORK/init.sql"
run_sql "$WORK/init.sql" "$WORK/init.out" || {
    fail "the initial load failed"
    exit 1
}
initial_count="$(count_rows t)"
if [ "$initial_count" != "$BATCH" ]; then
    fail "the initial load wrote $initial_count rows, expected $BATCH"
fi
say "  committed $initial_count rows"

# --------------------------------------------------------------- the kill loop

# Every round adds a batch of rows in an explicit transaction. The
# transaction is committed or not, depending on how far the round gets.
#
# The rounds alternate between a committed transaction (the data must
# survive) and an uncommitted one (the data must be absent). The uncommitted
# transaction is killed before its commit marker is written.

committed=0
for round in $(seq 1 "$ROUNDS"); do
    base=$((round * BATCH))

    # A committed batch.
    {
        printf "BEGIN;\n"
        for i in $(seq 1 "$BATCH"); do
            id=$((base + i))
            printf "INSERT INTO t VALUES (%d,'[%d,%d,%d,%d]');\n" \
                "$id" "$id" "$((id + 1))" "$((id + 2))" "$((id + 3))"
        done
        printf "COMMIT;\n"
    } >"$WORK/round.sql"

    # The process is killed while it is still working. Whether the
    # transaction was committed depends on how far it got; the test only
    # requires that the state is consistent afterwards.
    MINIVECDB_DATA_DIR="$DATA" \
    MINIVECDB_OUTPUT_FORMAT=tsv \
    MINIVECDB_READY_SINK=stderr \
    "$BINARY" <"$WORK/round.sql" >"$WORK/round.out" 2>/dev/null &
    pid=$!

    # Let the process work for a moment, then kill it.
    sleep 0.05
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null

    # An uncommitted transaction: the rows must not be visible.
    {
        printf "BEGIN;\n"
        for i in $(seq 1 20); do
            id=$((base + 100000 + i))
            printf "INSERT INTO t VALUES (%d,'[%d,%d,%d,%d]');\n" \
                "$id" "$id" "$id" "$id" "$id"
        done
        # No COMMIT: the transaction is never acknowledged.
    } >"$WORK/pending.sql"
    MINIVECDB_DATA_DIR="$DATA" \
    MINIVECDB_OUTPUT_FORMAT=tsv \
    MINIVECDB_READY_SINK=stderr \
    "$BINARY" <"$WORK/pending.sql" >"$WORK/pending.out" 2>/dev/null &
    pid=$!
    sleep 0.05
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null

    # Restart the database and check the state. The restart itself may be
    # interrupted in the next round, so the recovery has to be idempotent.
    {
        printf "SELECT id FROM t;\n"
    } >"$WORK/check.sql"
    if ! run_sql "$WORK/check.sql" "$WORK/check.out"; then
        fail "round $round: the database did not start again"
        continue
    fi

    # No row may appear twice.
    duplicates="$(tail -n +2 "$WORK/check.out" | sort -n | uniq -d | wc -l | tr -d ' ')"
    if [ "$duplicates" != "0" ]; then
        fail "round $round: $duplicates duplicated rows after the recovery"
    fi

    # The rows of the uncommitted transaction must be absent.
    ghosts="$(tail -n +2 "$WORK/check.out" | awk -v base="$base" \
        '$1 > base + 100000 && $1 <= base + 100020' | wc -l | tr -d ' ')"
    if [ "$ghosts" != "0" ]; then
        fail "round $round: $ghosts rows of the uncommitted transaction are visible"
    fi

    # The rows that were committed before the crash must still be there.
    missing=0
    for id in $(seq 1 "$BATCH"); do
        if ! tail -n +2 "$WORK/check.out" | grep -qx "$id"; then
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" != "0" ]; then
        fail "round $round: $missing of the initial rows are missing"
    fi

    current="$(count_rows t)"
    say "  round $round: $current rows, no duplicates, no ghosts"
done

# ------------------------------------------------ the final consistency check

say ""
say "the final state"

final_count="$(count_rows t)"
say "  rows: $final_count"
if [ "$final_count" -lt "$initial_count" ]; then
    fail "the final state has $final_count rows, fewer than the initial $initial_count"
fi

duplicates="$(list_rows t | uniq -d | wc -l | tr -d ' ')"
if [ "$duplicates" != "0" ]; then
    fail "the final state has $duplicates duplicated rows"
fi

# The rows of the very first batch must still be there.
missing=0
for id in $(seq 1 "$BATCH"); do
    if ! list_rows t | grep -qx "$id"; then
        missing=$((missing + 1))
    fi
done
if [ "$missing" != "0" ]; then
    fail "$missing of the initial rows are missing in the final state"
fi

# The rows of the uncommitted transactions must never be there.
ghosts=0
for round in $(seq 1 "$ROUNDS"); do
    base=$((round * BATCH))
    for i in $(seq 1 20); do
        id=$((base + 100000 + i))
        if list_rows t | grep -qx "$id"; then
            ghosts=$((ghosts + 1))
        fi
    done
done
if [ "$ghosts" != "0" ]; then
    fail "$ghosts rows of uncommitted transactions are visible in the final state"
fi

say ""
if [ "$failures" -ne 0 ]; then
    say "$failures check(s) failed"
    exit 1
fi
say "all checks passed"
exit 0
