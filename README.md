# MiniVectorDB

A small vector database: a SQL subset over tables that hold a vector column,
with an exact and an approximate TopK search over that column.

The server speaks the **JSON Lines protocol** of the competition over
standard input and standard output. It has no external dependencies - the
crate is written against the Rust standard library alone, so it builds
without network access.

## The protocol

The judge starts the process and talks to it over the standard streams. One
JSON object per line goes in, one JSON object per line comes out.

### The handshake

The server opens the data files, finishes the recovery and then writes one
line to standard output and flushes it, before it reads the first request:

```json
{"type":"ready"}
```

### The requests

```json
{"id":1,"op":"execute","sql":"SELECT id FROM docs ORDER BY id;","search_mode":"exact"}
{"id":2,"op":"shutdown"}
```

`id` is echoed back with the very same JSON type. `op` is `execute` or
`shutdown`. `search_mode` is `exact` or `ann`.

### The answers

```json
{"id":1,"ok":true,"columns":["id"],"rows":[[1],[2]]}
{"id":2,"ok":true,"affected_rows":1}
{"id":3,"ok":false,"error":{"code":"duplicate_key","message":"Duplicate entry '1' for key 'PRIMARY'"}}
```

`ok` is always a JSON boolean. A query carries its `columns` and its `rows`,
even when the result set is empty. A modification carries the number of rows
it changed. A failure carries an `error` with a non-empty `code`; the judge
does not look at the wording of the message.

A failing statement does not end the session: the failure is reported and the
server waits for the next request.

### The shutdown

The `shutdown` request rolls back an open transaction, flushes the committed
data to disk and answers `{"id":N,"ok":true}`. Nothing is written to standard
output after that answer, and the process ends with the code 0.

## Building

```sh
cd minivectordb
cargo build --release --offline --locked
```

The resulting binary is `minivectordb/target/release/minivectordb`.

Rust 1.98 or newer is recommended, matching the container image. The crate
has no dependencies, so `--offline` succeeds on an isolated builder.

## Running

```sh
minivectordb --data-dir /data
```

### Options

Every option has an environment variable of the same meaning. A flag on the
command line takes precedence over the environment variable.

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--data-dir` | `MINIVECDB_DATA_DIR` | `/data` | Directory that holds the database files. |
| `--mode` | `MINIVECDB_MODE` | `exact` | The search mode used when a request does not name one. |
| `--durability` | `MINIVECDB_DURABILITY` | `os` | `os` leaves the durability to the operating system, `full` also flushes the data files at every commit. |
| `--buffer-pool-mb` | `MINIVECDB_BUFFER_POOL_MB` | `64` | The memory budget of the block cache, in megabytes. |
| `--vector-block-rows` | `MINIVECDB_VECTOR_BLOCK_ROWS` | `4096` | The number of rows in one vector block. |
| `--ann-nlist` | `MINIVECDB_ANN_NLIST` | `64` | The number of coarse cells of a vector index. |
| `--ann-nprobe` | `MINIVECDB_ANN_NPROBE` | unlimited | The number of cells an approximate search visits. Without it the search is exact. |

### Container

```sh
docker build -t minivectordb .
docker run --rm -i -v "$PWD/data:/data" minivectordb --data-dir /data
```

The image holds the binary and nothing else, so it needs no network access at
run time. It starts the server in the JSON Lines mode by default, because
that is the mode the judge drives.

## The SQL subset

### Schema

```sql
CREATE TABLE [IF NOT EXISTS] t (
    id BIGINT PRIMARY KEY,
    category VARCHAR(16),
    embedding VECTOR(128)
);
CREATE VECTOR INDEX idx_t ON t(embedding);
```

`CREATE TABLE IF NOT EXISTS` succeeds when the table already exists and leaves
it unchanged. A table without a vector column is accepted, but a vector search
needs one.

### Data

```sql
INSERT INTO t VALUES (1, 'a', '[0.1,0.2,...]');
UPDATE t SET category = 'b' WHERE id = 1;
DELETE FROM t WHERE id = 1;
```

A statement outside an explicit transaction is its own transaction and takes
effect at once.

### Transactions

```sql
BEGIN;
INSERT INTO t VALUES (2, 'c', '[0.3,0.4,...]');
COMMIT;   -- or ROLLBACK;
```

The changes of a transaction are held in memory until it commits, so a
rollback is exact. A statement that fails inside a transaction undoes only
its own changes: the changes of the statements that ran before it stay, and
the transaction can continue. When the session ends while a transaction is
open, the transaction is rolled back.

### Queries

```sql
SELECT id, category FROM t WHERE category = 'a' ORDER BY id LIMIT 10;
SELECT id, L2_DISTANCE(embedding, '[0.1,0.2,...]') AS distance
    FROM t WHERE category = 'a' ORDER BY distance LIMIT 10;
```

The supported comparisons are `=`, `<>`, `<`, `<=`, `>`, `>=`, the boolean
operators `AND` and `OR` with parentheses, `ORDER BY` with `ASC` and `DESC`,
and `LIMIT`. The ordering breaks a tie by the primary key, ascending.

`L2_DISTANCE` over a vector column, ordered ascending and limited, is the
shape of a vector search. Such a statement is answered by the vector search
path, which scans the table in blocks rather than row by row. The filter is
applied before the answer is chosen, so a filtered search returns the nearest
rows *among the rows that satisfy the filter*.

## Errors

A failure is reported through the `error` field of the answer, and the server
continues with the next request. The `code` names the kind of the failure:

| Code | Meaning |
|---|---|
| `syntax_error` | The statement could not be parsed. |
| `unsupported_sql` | The statement is outside the supported subset. |
| `no_such_table` | The table does not exist. |
| `table_exists` | The table already exists. |
| `no_such_column` | The column does not exist. |
| `duplicate_key` | The primary key is already taken. |
| `dimension_mismatch` | The vector does not have the number of components of its column. |
| `data_error` | The value cannot be converted to the type of its column. |
| `value_out_of_range` | The value does not fit its column. |
| `transaction_state` | The transaction cannot run in its current state. |
| `internal_error` | An internal failure. |

The numeric MySQL code of every failure is kept as well and appears in the
diagnostics on standard error.

## What is stored

```text
<data-dir>/wal.dat                the write-ahead log
<data-dir>/catalog.dat           the table definitions
<data-dir>/<table>/meta.dat       the row directory
<data-dir>/<table>/col.dat        the scalar columns, as row images
<data-dir>/<table>/vec.dat        the vector column
<data-dir>/<table>/index.dat      the marker that records a vector index
```

The vector column is stored once, in `vec.dat`. The row image of a row in
`col.dat` leaves the vector out and marks its place, so a column keeps its
index. The write-ahead log holds the complete image, so a log record stays
self contained and the recovery does not depend on the order in which the
data files were flushed.

The design of the storage, the index and the recovery is described in
[docs/design.md](docs/design.md).

## Tests

```sh
cd minivectordb && cargo test --release --offline --locked   # the unit and protocol tests
python3 tools/run_public_cases.py                            # the public cases
python3 tools/run_public_cases.py --mode ann                 # the public cases, approximate mode
python3 tools/selftest.py                                    # the differential tests
bash tools/smoke.sh                                          # the smoke tests
bash tools/killtest.sh                                       # the crash recovery tests
python3 tools/bench.py                                       # the performance measurement
python3 tools/perf_compare.py --before A --after B           # the performance comparison
```

The official checker of the test package can be run against the image as
well:

```sh
python3 protocol_check/run.py --image <image> --output <new-directory>
```