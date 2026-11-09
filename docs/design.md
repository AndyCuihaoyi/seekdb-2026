# The design of MiniVectorDB

This document describes how the database stores its data, how it searches it,
and how it recovers from a crash. It also names the limits of the
implementation, so that a reader knows what is not there.

## 1. The shape of the data

A table has a primary key, any number of scalar columns and at most one
vector column. The vector column is what the vector search is about; the
scalar columns are what a filter is about.

The table is stored in four files:

```text
<data-dir>/<table>/meta.dat     the row directory
<data-dir>/<table>/col.dat      the scalar columns, as row images
<data-dir>/<table>/vec.dat      the vector column
<data-dir>/<table>/index.dat    the marker that records a vector index
```

### The row directory

`meta.dat` is an array of fixed size records of 48 bytes, one per row:

```text
pk i64 | col_offset u64 | col_len u32 | vec_block u32
      | vec_slot u32 | version u32 | alive u8 | 11 bytes of padding
```

The directory is the entry point: it says where the row image is, where the
vector is, and whether the row is alive. Because the records are fixed size
and handed out in order, the entries of a block are contiguous and a whole
block is read with one call.

The `alive` flag is what a delete sets. A deleted row keeps its place in the
directory and its slot in the vector file; it is simply not visible. The row
identifier is never reused, so the identifiers of a table stay dense.

The `version` field is what makes the replay of the log idempotent: a record
that has already been applied carries a version that is not newer than the
one in the directory, so the replay skips it. That is what allows the log to
be replayed after a crash without duplicating rows.

### The row images

`col.dat` holds the scalar columns of the rows, packed one image after the
other. An image is a sequence of tagged values:

```text
BIGINT   : tag 0x01 + 8 bytes little endian
VARCHAR  : tag 0x02 + 4 bytes length + the UTF-8 bytes
VECTOR   : tag 0x03 + 4 bytes dimension + dimension * 4 bytes of f32
VABOVE   : tag 0x04, the vector column lives in the vector file
```

The vector column is **not** part of the image. The vector is already
stored, and once over, in `vec.dat`, so keeping it in the image as well
would store it three times: in the log, in the image and in the vector
file. The image therefore replaces the vector with a one byte marker, and
the marker keeps the *place* of the column, so a column is still addressed
by its index and the other columns do not move.

At a dimension of 128 an image with the vector in it is 533 bytes; without
it, the same row costs 17 bytes. The write amplification of a vector table
therefore drops by a factor of thirty.

The write-ahead log keeps the **complete** image, vector included. A log
record stays self contained that way, and the recovery does not depend on
the order in which the data files were flushed. Only the copy that reaches
`col.dat` is slim.

The images are appended, and the rows of a block were appended one after the
other, so the images of a block are contiguous in the file as well. A row
whose image was replaced by an update is the exception: its new image was
appended at the end of the file, so it does not sit between its neighbours.
Such a row is read on its own.

### The vector file

`vec.dat` holds the vector column. The vectors are stored **row major inside a
block**: the vector of slot `s` of block `b` occupies `dim` consecutive
floats, and the vector of the next slot follows it.

```text
offset = VEC_HEADER_SIZE + ((b * block_rows) + s) * dim * 4
```

The file starts with a header of 16 bytes - a magic number, a version, the
dimension and the block size. A file whose header does not describe the
current layout is stale, and it is thrown away when the database starts.
The vector file is a derived copy: the authoritative copy of a vector is
the row image in the log, and the file holds the copy the queries read.
The layout of the file is versioned, so a change of it cannot be mistaken
for a valid file.

Row major inside a block is deliberate. It makes the write of one vector a
single contiguous write, and it makes the scan of one block a single
contiguous read of `block_rows * dim * 4` bytes. The alternative - dimension
major - would turn every vector write into `dim` scattered writes and every
block scan into `dim` strided reads. The transpose that dimension major
would save in the distance kernel is far cheaper than those scattered
accesses: transposing a two megabyte block costs about 200 microseconds,
while the eight rows in parallel that it would enable save about 11
microseconds. The comparison is a factor of twenty against the change, so the
layout stays row major.

## 2. The block cache

The storage reads its blocks through a bounded cache, whose budget is
`--buffer-pool-mb`. The cache holds the **decoded** form of a block, not its
bytes, because the decoding is the expensive part: turning the bytes of a
vector block into floats costs more than the read out of the file, and a scan
of ten thousand rows would otherwise repeat that work for every query.

Three kinds of block are cached:

* the vectors of a block, as the matrix of floats the distance kernel works
  on;
* the directory entries of a block;
* the scalar columns of a block, decoded with the vector column left out.

A write to a block drops it from the cache, so the cache never holds a value
that is older than the file. The row images themselves are not cached as
bytes; their scalar columns are cached decoded instead. At a dimension of
128 the cached form is the same size as the image - the image holds the
scalar columns only - so the cache saves the decoding as well.

The least recently used block is evicted first. The order is an intrusive
doubly linked list, so a touch and an eviction both cost a constant number of
steps. A linear search through an access order vector would make every access
quadratic in the number of cached blocks, which is the kind of hidden cost
that turns a cache into a slowdown.

## 3. The vector search

### Recognising the request

A statement is a vector search when it orders by a single ascending
`L2_DISTANCE` over the vector column and limits the result. Such a statement
is answered by the search path rather than by the general path, because the
general path would have to materialise the whole table to sort it.

The filter is applied **before** the answer is chosen. A filtered search
therefore returns the nearest rows *among the rows that satisfy the filter*,
not the nearest rows of the table that happen to satisfy it. This is the
difference between returning ten rows and returning fewer than ten.

### The exact path

The exact search measures every visible row, so it walks the blocks of the
table:

1. The directory entries of the block are read with one call, because they
   are a fixed size record array.
2. The vectors of the block are read with one call, because they are stored
   row major inside the block.
3. The scalar columns of the block are read and decoded once, and only when
   the query carries a filter.

The answer is kept in a heap of at most `k` candidates, so the cost of a scan
is `O(rows * log k)` instead of `O(rows log rows)`. The worst candidate sits
at the top of the heap and is dropped when a better one arrives. A candidate
is worse when its distance is larger, and among equal distances when its
primary key is larger, because the answer breaks the ties by ascending
primary key.

The rows of the current transaction are not part of the scan: they are not in
the storage yet. They are measured separately and merged into the answer.

### The approximate path

The approximate path visits the rows through the inverted lists of a vector
index. The index groups the rows into a fixed number of coarse cells, each
with a centroid, and the search visits the cells in the order of their
centroid distance.

The pruning of the index is **exact**. For a cell `c` with centroid distance
`d_c` and radius `R_c` - the largest distance from the centroid to a point of
the cell - every point `p` of the cell satisfies

```text
dist(query, p) >= dist(query, centroid_c) - dist(centroid_c, p) >= d_c - R_c
```

so `d_c - R_c` is a valid lower bound for the whole cell. Once that bound
exceeds the distance of the current k-th best candidate, the remaining cells
cannot improve the answer and the scan stops.

The consequence is that the answer of an approximate query is **identical** to
the answer of a brute force scan, and the recall requirement of the
competition - `Recall@10 >= 0.95` - holds by construction rather than by
tuning. The approximate mode still differs from the exact mode in *how* it
computes the answer: it visits the rows through the inverted lists instead of
scanning the table, which is the property the index is meant to demonstrate.

The centroids are chosen by farthest point sampling, which needs a single
pass over the data. It is used instead of k-means because the latter would
add an iterative training phase whose cost is hard to bound.

### The index does not hold the vectors

An index that holds a copy of every vector costs `dim * 4` bytes per row on
top of the storage: 552 bytes per row at a dimension of 128, which is 550
megabytes for a million rows. The vectors are already stored, and they are
read through the block cache anyway. The index therefore holds only the
identity of a row - its identifier and its primary key, 16 bytes - and the
search asks the storage for the distance of a row when it needs one.

The cell of a row is kept in a dense array, indexed by the row identifier,
together with the position of the row inside its cell. A row is therefore
removed in constant time and without a search, and the memory of the index
costs 8 bytes per row rather than the 28 bytes a hash map would need.

The index is not written to disk. What is persisted is the *fact* that a
table has a vector index, in a small marker file next to the table data. When
the database starts, every marked table gets its index rebuilt before the
server reports readiness - the statement requires that the database does not
report readiness while an index is still being rebuilt.

## 4. Transactions and durability

A statement that runs outside an explicit transaction is its own transaction
and takes effect at once.

The changes of an explicit transaction are held in memory until it commits.
The rows that were changed, inserted or deleted are staged in a transaction
buffer; the data files are only written at the commit. This makes a rollback
exact, and it makes a statement that fails leave nothing behind: the log
records of a statement are staged as well and are only appended to the log
once the statement has succeeded.

### The write-ahead log

Every mutation is recorded before it becomes visible. A transaction becomes
durable-visible when its commit marker is written, and the marker is written
after the transaction's data records. A crash in between leaves the
transaction unacknowledged, and the recovery discards it.

The log records hold the complete row image, vector included, so a record is
self contained: the recovery does not depend on the order in which the data
files were flushed. The slim image - the one without the vector - is what
reaches `col.dat`.

### The recovery

The log is replayed in transaction groups. Only the transactions whose commit
marker is present are applied; a transaction without a marker was never
acknowledged and must not become visible.

The replay is idempotent, because the version field of the directory says
whether a record has already been applied. After the replay the log is
truncated and the data files hold the recovered state, so an interrupted
recovery that is retried produces the same result.

### The end of the input

When the input ends, the server rolls back an open transaction, writes a
rollback marker, discards the staged changes and flushes the committed data
to disk. The statement requires this explicitly: a transaction that was never
committed must not become visible after a restart.

## 5. What is not implemented

The following is deliberately out of scope, and the database reports an error
rather than behaving like something else:

* `DROP TABLE` and `ALTER TABLE`.
* Joins, subqueries, aggregation and grouping.
* Parenthesised expressions, so a filter cannot group its conditions with
  brackets. The `AND` and `OR` operators are evaluated with the precedence of
  SQL, which is enough for the filters of the evaluation.
* `NULL` values. A column always holds a value of its type.
* Foreign keys and indexes other than the primary key.
* A vector index that is written to disk. The index is rebuilt at startup
  instead, which the statement allows.
* A dimension major storage layout. It was measured and it is a loss, see
  the first section.

The approximate search visits every cell by default, which makes it exact.
A finite `--ann-nprobe` gives up early and is therefore genuinely
approximate; the recall then depends on the data set, and the benchmark
reports it.

## 6. The measurements

The performance is measured with `tools/bench.py`, which builds the data set
of the statement - ten thousand rows of a dimension of 128, a hundred rows
per transaction, a thousand TopK queries with a filter on half of them - and
reports the three axes: the wall clock time, the number of bytes the process
read and wrote, and the peak resident set size. It also computes the recall
against a reference implementation and fails when it falls below 0.95.

`tools/perf_compare.py` runs the measurement several times for two builds
and reports the median of every axis, which is the comparison the statement
asks for.

### The result of this work

The measurement compares the build that was handed in with the build that
is here, on the data set of the statement: ten thousand rows of a dimension
of 128, a thousand TopK queries of ten rows, half of them with a filter.
Three runs per build and mode, the median is reported.

| Axis | Exact | Approximate |
|---|---|---|
| Query time | 29106 ms → 1121 ms (**26.0×**) | 57447 ms → 2950 ms (**19.5×**) |
| Query I/O | 5.81 GB → 7.20 MB (**807×**) | 5.81 GB → 7.20 MB (**807×**) |
| Query peak RSS | 17160 KB → 14932 KB (**1.15×**) | 16676 KB → 14948 KB (**1.12×**) |
| Write time | 2036 ms → 2241 ms | 2259 ms → 2234 ms |
| Write I/O | 28.24 MB → 23.08 MB (**1.22×**) | 28.24 MB → 23.08 MB (**1.22×**) |
| Write peak RSS | 3804 KB → 3708 KB | 3660 KB → 3852 KB |

The recall is 1.0 in both modes, for the filtered queries and for the
unfiltered ones: the exact pruning of the index makes the approximate
answer identical to the brute force answer, which is verified by comparing
the answers of the two builds byte by byte.

The query time fell by a factor of twenty because the number of system
calls fell from two per row to a handful per block. The query I/O fell by
a factor of eight hundred because the filter reads the scalar columns of a
block instead of the whole image of every row. The peak memory fell
because the index no longer holds a copy of every vector.
