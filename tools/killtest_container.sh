#!/usr/bin/env bash
# Crash recovery test of MiniVectorDB, inside the container image.
#
# This is the container counterpart of `tools/killtest.sh`. The test of the
# host kills the database process; the test of the container kills the
# container, which sends SIGKILL to the process inside it - the same crash,
# seen from the outside.
#
# The data directory is a host directory that is mounted at /data, so that
# it survives the death of the container.
#
# The database speaks the JSON Lines protocol of the competition, so the
# statements are converted into protocol requests before they are fed to the
# container, and the answers are read back as protocol messages. The
# assertions are the ones of `tools/killtest.sh`, unchanged.
#
#   MINIVECDB_CONTAINER_IMAGE=mvdb-jsonl:local bash tools/killtest_container.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${MINIVECDB_CONTAINER_IMAGE:-mvdb-final}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mvdb-killtest-container-XXXXXX")"
DATA="$WORK/data"

# The number of kill rounds.
ROUNDS="${ROUNDS:-12}"

# The number of rows committed in every round.
BATCH="${BATCH:-200}"

failures=0
container_seq=0

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

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    say "error: the image $IMAGE does not exist"
    say "       run 'docker build -t $IMAGE .' first"
    exit 2
fi

mkdir -p "$DATA"

# The containers run as the user of the host, so that the files they write
# belong to that user and the host can remove them again.
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

# ---------------------------------------------------------------- the helpers

# The statements of a file, turned into the requests of the protocol.
to_jsonl() {
    local sql_file="$1"
    local jsonl="$2"
    python3 "$ROOT/tools/sql2jsonl.py" "$sql_file" "$jsonl" || return 1
}

# Run one SQL statement in a container and write the answer to the file of
# the second argument. The container is given a name, so that it can be
# killed.
run_sql() {
    local sql_file="$1" out_file="$2"
    local jsonl="$WORK/feed.jsonl"
    to_jsonl "$sql_file" "$jsonl" || return 1
    local name
    container_seq=$((container_seq + 1))
    name="mvdb-killtest-$$-$container_seq"
    docker run --rm -i \
        --name "$name" \
        -v "$DATA":/data \
        -u "$HOST_UID:$HOST_GID" \
        -e MINIVECDB_MODE=exact \
        "$IMAGE" --data-dir /data \
        <"$jsonl" >"$out_file" 2>/dev/null
}

# Start a database in a container and kill it after a moment. The rows of
# the transaction are committed or not, depending on how far it got.
kill_during() {
    local sql_file="$1"
    local jsonl="$WORK/feed.jsonl"
    to_jsonl "$sql_file" "$jsonl" || return 1
    local name
    container_seq=$((container_seq + 1))
    name="mvdb-killtest-$$-$container_seq"
    docker run --rm -i \
        --name "$name" \
        -v "$DATA":/data \
        -u "$HOST_UID:$HOST_GID" \
        -e MINIVECDB_MODE=exact \
        "$IMAGE" --data-dir /data \
        <"$jsonl" >/dev/null 2>/dev/null &
    local pid=$!
    # Let the process work for a moment, then kill it.
    sleep 0.05
    docker kill --signal=KILL "$name" >/dev/null 2>&1
    wait "$pid" 2>/dev/null
}

# Count the rows of a table with a query.
count_rows() {
    local table="$1"
    local sql_file="$WORK/count.sql"
    local out_file="$WORK/count.out"
    printf 'SELECT id FROM %s;\n' "$table" >"$sql_file"
    run_sql "$sql_file" "$out_file" || { echo "-1"; return; }
    # The answer is one JSON object; the rows are in its `rows` field.
    python3 - "$out_file" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8").read()
count = 0
for line in text.splitlines():
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if message.get("type") == "ready":
        continue
    if message.get("ok") is True:
        count += len(message.get("rows") or [])
print(count)
PY
}

# The identifiers of the rows of a table, sorted.
list_rows() {
    local table="$1"
    local sql_file="$WORK/list.sql"
    local out_file="$WORK/list.out"
    printf 'SELECT id FROM %s;\n' "$table" >"$sql_file"
    run_sql "$sql_file" "$out_file" || return
    python3 - "$out_file" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8").read()
ids = []
for line in text.splitlines():
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if message.get("type") == "ready":
        continue
    for row in message.get("rows") or []:
        ids.append(str(row[0]))
for value in sorted(ids, key=int):
    print(value)
PY
}

say "MiniVectorDB crash recovery test (container)"
say "  image  : $IMAGE"
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

    kill_during "$WORK/round.sql"

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
    kill_during "$WORK/pending.sql"

    # Restart the database and check the state. The restart itself may be
    # interrupted in the next round, so the recovery has to be idempotent.
    {
        printf "SELECT id FROM t;\n"
    } >"$WORK/check.sql"
    if ! run_sql "$WORK/check.sql" "$WORK/check.out"; then
        fail "round $round: the database did not start again"
        continue
    fi

    # Collect the identifiers of the rows that are visible.
    list_rows t >"$WORK/ids.txt"

    # No row may appear twice.
    duplicates="$(sort -n "$WORK/ids.txt" | uniq -d | wc -l | tr -d ' ')"
    if [ "$duplicates" != "0" ]; then
        fail "round $round: $duplicates duplicated rows after the recovery"
    fi

    # The rows of the uncommitted transaction must be absent.
    ghosts="$(awk -v base="$base" \
        '$1 > base + 100000 && $1 <= base + 100020' "$WORK/ids.txt" | wc -l | tr -d ' ')"
    if [ "$ghosts" != "0" ]; then
        fail "round $round: $ghosts rows of the uncommitted transaction are visible"
    fi

    # The rows that were committed before the crash must still be there.
    missing=0
    for id in $(seq 1 "$BATCH"); do
        if ! grep -qx "$id" "$WORK/ids.txt"; then
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

list_rows t >"$WORK/final_ids.txt"

duplicates="$(sort -n "$WORK/final_ids.txt" | uniq -d | wc -l | tr -d ' ')"
if [ "$duplicates" != "0" ]; then
    fail "the final state has $duplicates duplicated rows"
fi

missing=0
for id in $(seq 1 "$BATCH"); do
    if ! grep -qx "$id" "$WORK/final_ids.txt"; then
        missing=$((missing + 1))
    fi
done
if [ "$missing" != "0" ]; then
    fail "$missing of the initial rows are missing in the final state"
fi

ghosts=0
for round in $(seq 1 "$ROUNDS"); do
    base=$((round * BATCH))
    for i in $(seq 1 20); do
        id=$((base + 100000 + i))
        if grep -qx "$id" "$WORK/final_ids.txt"; then
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
