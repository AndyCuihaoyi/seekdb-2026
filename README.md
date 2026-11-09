# MiniVectorDB

A small vector database: a SQL subset over tables that hold a vector column,
with an exact and an approximate TopK search over that column.

The server reads SQL statements from standard input and writes the results to
standard output. It has no external dependencies - the crate is written
against the Rust standard library alone, so it builds without network access.

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

The server reads statements from standard input, one per line or terminated by
a semicolon, and writes the results to standard output. When standard input
reaches its end, the server rolls back an open transaction, flushes the
committed data to disk and exits.

### Options

Every option has an environment variable of the same meaning. A flag on the
command line takes precedence over the environment variable.

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--data-dir` | `MINIVECDB_DATA_DIR` | `/data` | Directory that holds the database files. |
| `--output-format` | `MINIVECDB_OUTPUT_FORMAT` | `tsv` | How results are rendered: `tsv`, `json` or `jsonl`. |
| `--ready-sink` | `MINIVECDB_READY_SINK` | `stderr` | Where the readiness marker is written: `stdout`, `stderr` or `none`. |
| `--ready-token` | `MINIVECDB_READY_TOKEN` | `ready` | The word the readiness marker holds. |
| `--mode` | `MINIVECDB_MODE` | `exact` | `exact` searches every row, `ann` uses the approximate index when one exists. |
| `--durability` | `MINIVECDB_DURABILITY` | `os` | `os` leaves the durability to the operating system, `full` also flushes the data files at every commit. |
| `--buffer-pool-mb` | `MINIVECDB_BUFFER_POOL_MB` | `64` | The memory budget of the block cache, in megabytes. |
| `--vector-block-rows` | `MINIVECDB_VECTOR_BLOCK_ROWS` | `4096` | The number of rows in one vector block. |
| `--ann-nlist` | `MINIVECDB_ANN_NLIST` | `64` | The number of coarse cells of a vector index. |
| `--ann-nprobe` | `MINIVECDB_ANN_NPROBE` | unlimited | The number of cells an approximate search visits. Without it the search is exact. |

### Container

```sh
docker build -t minivectordb .
docker run --rm -v "$PWD/data:/data" minivectordb --data-dir /data
```

The image holds the binary and nothing else, so it needs no network access at
run time.

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
rollback is exact and a failed statement leaves nothing behind. When the input
ends while a transaction is open, the transaction is rolled back.

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

An error is reported on standard output as `ERROR <code>: <message>`, and the
server continues with the next statement. The codes are the ones of MySQL:
`1064` for a syntax error, `1146` for an unknown table, `1050` for a table
that already exists, `1054` for an unknown column, `23000` for a duplicate
key and `1366` for a value that cannot be converted.

## What is stored

```text
<data-dir>/wal.dat                the write-ahead log
<data-dir>/catalog.dat           the table definitions
<data-dir>/<table>/meta.dat       the row directory
<data-dir>/<table>/col.dat        the scalar columns, as row images
<data-dir>/<table>/vec.dat        the vector column
<data-dir>/<table>/index.dat      the marker that records a vector index
```

The design of the storage, the index and the recovery is described in
[docs/design.md](docs/design.md).

## Tests

```sh
cd minivectordb && cargo test --release --offline --locked   # the unit tests
python3 tools/run_public_cases.py                            # the public cases
python3 tools/run_public_cases.py --mode ann                 # the public cases, approximate mode
python3 tools/selftest.py                                    # the differential tests
bash tools/smoke.sh                                          # the smoke tests
bash tools/killtest.sh                                       # the crash recovery tests
python3 tools/bench.py                                       # the performance measurement
python3 tools/perf_compare.py --before A --after B           # the performance comparison
```
