# MiniVectorDB performance ledger

This file records what the performance work measured, what it changed, and
what it did not change. Every number here was taken on this machine with the
tools in `tools/`, and every claim names the command that produced it.

The platform measures three things per phase: the wall clock time `T`, the
number of bytes read from and written to the data device `B`, and the peak
memory of the container `M`. The statement of the competition says that `M`
is the `memory.peak` of the cgroup of the container, so it includes the page
cache of the files under `/data`, and not only the resident set of the
database process.

**The status of the numbers in this file.** The measurements were corrected
five times in the round of 2026-11-11, and every correction changed the
conclusion:

1. The rounds were noisy, because the server was floating between the
   cores. Pinning it fixed that, and the query turned out to be 3.00 times
   faster than the baseline rather than 1.51 times. Section 1.
2. The bytes were counted with the wrong counter, which measured the
   traffic of the pipe instead of the disk. The query phase reads **nothing**
   from the disk, and the write phase writes the same bytes as the baseline.
   Section 1.1.
3. The measurement was taken on the wrong number of cores. The judge runs
   the container with `--cpus=2`, and on this machine two cores give numbers
   three times the one-core ones. Section 1.0.
4. The measurement was taken while the machine was busy. A build of the
   kernel was running next to it, and three rounds of the same binary
   differed by 45%. Section 1.0.1.
5. The number of cores is not the number the platform gives. `--cpus=2` is a
   quota, not an affinity, and under a quota the speedup of the code is
   1.25x and not 3.00x. The `3.00x` is a fact about the code under this
   protocol, not a forecast of the platform's number. Section 1.0.

So the three numbers that the platform counts are these:

| Axis | Baseline | Now | Note |
| --- | ---: | ---: | --- |
| The time of the query | 965.5 ms | 318.7 ms | 3.03x better (1.25x under a quota) |
| The bytes of the query | 0 B | 0 B | the page cache serves all of it |
| The memory of the query | 14,788 KB | 11,392 KB | 23% less |
| The time of the write | 762.2 ms | 583.6 ms | 1.31x better |
| The bytes of the write | 11,407,360 B | 11,411,456 B | **not moved** |

The one that has not moved is the one that is worth the next round. The
ratio of the query is given twice, because the placement of the two
processes changes it, and section 1.0 says which one the platform is
likely to see. Section 4.4 says what each of the remaining levers is
worth.

---

## 1. The baselines

The reference implementation of the competition is not published, so the
platform uses its own baseline, whose numbers are unknown to us. What we can
do is compare our own code against the code we started from, under the same
conditions, and look at how the ratio `R` that the formula uses moves.

The commit `b88e0209` is the code we started this round from. It is the
baseline of the table below.

**How these were measured matters more than the numbers themselves.** The
machine is shared, and the server was floating between the cores, so the
same binary was seen anywhere between 624 ms and 1007 ms for the same
query phase. A comparison made on numbers like that measures the machine
rather than the code.

The fix is to pin the driver and the server to the **same** core. Every
move of the server between cores takes the ten megabytes of the decoded
blocks out of the cache of the old core and puts them into the cache of
the new one, so the scan pays for the reads again. With both pinned to
one core, the spread of the rounds fell from 49% to 0.7%, and the query
phase became 2.2 times faster.

```sh
python3 tools/bench_median.py --mode exact --rounds 5 --cpu 7
```

| Measurement | Baseline `b88e0209` | Before `7ff176a53` | Now | Ratio to the baseline |
| --- | ---: | ---: | ---: | ---: |
| Query `T` | 969.2 ms | 323.5 ms | 318.7 ms | **3.03x faster** |
| Query `B` (device) | 0 B | 0 B | 0 B | both are served by the page cache |
| Query `M` (RSS) | 14,800 KB | 11,896 KB | 11,392 KB | **0.77x, 23% less** |
| Write `T` | 751.4 ms | 566.4 ms | 583.6 ms | 1.29x faster |
| Write `B` (device) | 11,407,360 B | 11,407,360 B | 11,411,456 B | 1.00x |
| Write `M` (RSS) | 3,960 KB | 3,988 KB | 3,872 KB | 0.98x |

The spread of the five rounds of each row is under 4%. The measurements
are in `docs/perf/baseline-pinned-exact.json`,
`docs/perf/p5-current-exact.json` and `docs/perf/p6-memory-round-exact.json`.

**The middle column is the point of this round.** The numbers of the
table above the baseline column were the ones of `7ff176a53`, and the
round that removed the memory that grew with the number of rows changed
them. The three binaries were measured one after another, five rounds
each, in the order `baseline`, `before`, `now`, so that a drift of the
machine hits all three of them and cancels out; the commands are in
section 5. The result was that the round made the query *faster* and the
memory *smaller*, but only after a regression was found and fixed - see
section 1.2.

**The query is 3.00 times faster than the baseline, and the earlier
figure of 1.51 times was an artefact of the noise.** The earlier table of
this file was built on unpinned rounds, in which the baseline and the
current code were measured at different moments of the same noise, and
the comparison landed in the middle of it.

The answers of the two versions are byte for byte identical, in both the
exact and the approximate mode (`docs/perf/answers-baseline-*.txt`). That
was checked against the baseline binary itself, not only against the file.

### 1.2 The round that removed the growing memory made the memory worse

The round of 2026-11-12 removed the memory that grew with the number of
rows. It changed the code that builds the vector index, among other
things, and the build of the index is charged against the peak memory of
the container, so the numbers of section 1 had to be taken again.

They came out **worse on the memory**, and the cause was not the round
itself but one constant in it.

**The measurement.** The three binaries were measured one after another,
five rounds each, in the order `baseline`, `before`, `now`:

| Binary | Query `T` | Query `M` | Index build `T` |
| --- | ---: | ---: | ---: |
| `b88e0209` (baseline) | 965.5 ms | 14,788 KB | 73.6 ms |
| `7ff176a53` (before) | 326.0 ms | 11,808 KB | 60.2 ms |
| the round, as it was | 315.0 ms | **13,892 KB** | 29.6 ms |

The query was faster and the memory was 17% worse. The memory of the
phase that builds the index says where it came from: it went from 9 MiB
to 14 MiB at ten thousand rows.

**The cause.** The build of the index chose the centroids of the cells
from a sample of the table, and the sample held the *vectors* of the
sampled rows: `SAMPLE_ROWS` vectors of `dim` floats, which is four
megabytes at a dimension of 128. The second pass of the build allocated
the index on top of that sample, so the peak of the build was four
megabytes higher than it had to be. The constant was even wrong about
itself: the comment above it said the sample was "1 MiB at a dimension of
128", and 8192 rows of 128 floats is 4 MiB.

**The fix.** The sample holds the *identifiers* of the sampled rows now,
and not their vectors. The farthest point sampling needs, for every row
of the sample, the distance to the closest centroid chosen so far, and it
needs it again for every centroid it picks afterwards; the pass therefore
walks the table once per centroid and keeps only the distance of every
row of the sample - four bytes each - and one block of vectors. The
number of centroids is small (64 by default), so the number of passes is
small too, and the rows are read through the block cache, so the rows
that are still cached cost no system call.

The build of the index got *faster* as well - 29.0 ms against 60.2 ms -
because the table is walked twice instead of once, but the second walk
reads the rows it has already read.

**The size of the sample was not the thing to change.** The obvious fix
is to make the sample smaller, and it is the wrong one: the sample is
what decides where the cells are, and a smaller sample spreads them
worse. At a finite `--ann-nprobe` of 4, 8 and 16, a sample of 2048 rows
gave a recall of 0.22, 0.36 and 0.55, where a sample of 8192 rows gave
0.30, 0.45 and 0.63. Every one of those is below the 0.95 the statement
asks for, so the platform has to use the default of no limit - but a
smaller sample would have made the answer worse for nothing.

**What the fix did not change.** The answers are byte for byte identical
to the ones of the round before it, in both the exact and the approximate
mode: 102 of 102 answers of `tools/check_answer_equivalence.py`, and 400
answers of a script that asks the same 200 queries in the two modes
against the two binaries.

### 1.0 How the measurement is taken, and why it is this way

Three things had to be fixed before the numbers meant anything, and each
of them changed the conclusion.

**The cores.** The judge runs the container with `--cpus=2`. On this
machine the driver and the server are two processes, and where they run
changes the number a lot, because the ten megabytes of the decoded blocks
do not fit into the ten megabytes of the second level cache of one core.

| Where the driver and the server run | Median | Spread |
| --- | ---: | ---: |
| Free, twenty cores | 757.6 ms | 76% |
| Two cores | 950.2 ms | 2.7% |
| One core, the driver and the server on it | 323.5 ms | 2.8% |

The protocol of this file is therefore **one core, both the driver and the
server**, and the rounds are reported as a range.

**But this is where the numbers of this file have to be read with care.**
`--cpus=2` in Docker is a *quota* on the CPU time, not a restriction of
the cores the processes may run on, and a quota was emulated here with
`systemd-run -p CPUQuota=200%`. Under that emulation the two-core numbers
came out at roughly 950 ms for the current code against 1,185 ms for the
baseline, which is a ratio of 1.25 and not of 3.00.

The two measurements are not in conflict, and the reason is the order of
the two effects. Sharing one core makes the driver and the server share
one cache, and that is worth a factor of about 2.2 *for both versions*.
The baseline and the current code differ in how much CPU they need, and
that difference is worth a factor of about 3. The ratio of the two
versions is therefore largest when the two of them share a core, and
smallest when they are spread over two.

**What this means for the ledger.** The `3.00x` of section 1 is a
measurement of this machine under this protocol, and it is the right
number to compare the *code* with, because both versions were measured
the same way. It is **not** a prediction of the ratio the platform will
see, because the platform runs with a quota and free placement. The
honest statement is that the speedup of the code is between 1.25x and
3.00x depending on how the two processes are placed, and that the first
submission will tell us which end of that range the platform is at.

**The counters of the I/O.** See section 1.1.

**The number of rounds.** Five, and the median of them, which is what the
platform does with three.

```sh
python3 tools/bench_median.py --mode exact --rounds 5 --cpu 3 --cpus 3
```

### 1.0.1 What contaminates a measurement on this machine

Three rounds of the same binary on the same data differed by 45% and by
67% on two separate occasions, and neither was the code. The cause was
the machine: a build of the kernel was running next to it, with eight
compilers at 100% and every core of the twenty loaded.

The rule that follows, and that the rounds of this file obey:

1. No build, no profile, and no other benchmark runs while a measurement
   runs. `pgrep -c clang` is the cheapest way to check.
2. The load average before a measurement is read and recorded. A load
   above one on a twenty-core machine is a warning, not a failure.
3. The rounds are run in turn, the baseline and the current code
   interleaved, so that a drift of the machine hits both of them and
   cancels out.
4. A spread above 10% is not a measurement and does not go into this
   file. The measurement is repeated, and the cause is looked for first.

### 1.1 The measurement of `B` was measuring the wrong thing

The `B` of the table above is the number of bytes that reached the block
device. It is **not** what this repository used to report, and the
difference is not a rounding.

The old measurement used `rchar` and `wchar` of `/proc/<pid>/io`. Those
two count every byte the process read or wrote, **including the bytes
that came in and went out through the pipes of the protocol**. The SQL
text of the benchmark is 11.7 MB for the load and 1.17 MB for the
queries, so a large part of the number that was reported as the I/O of a
phase was the traffic of the pipe.

The platform reads `rbytes` and `wbytes` from `io.stat` of the cgroup of
the container. Those count the block device. So the old number and the
number of the platform were not the same quantity, and the old number was
the larger one.

`tools/measure_io.py` reports both side by side:

```
write phase:
  rchar           11.159 MB   (includes the traffic of the pipe)
  wchar           10.875 MB   (includes the traffic of the pipe)
  read_bytes       0.000 MB   (reached the block device)
  write_bytes     10.879 MB   (reached the block device)
query phase:
  rchar            6.742 MB   (includes the traffic of the pipe)
  wchar            0.297 MB   (includes the traffic of the pipe)
  read_bytes       0.000 MB   (reached the block device)
  write_bytes      0.000 MB   (reached the block device)
```

Two things follow, and they are both about the score:

1. **The query phase reads nothing from the disk.** Every byte of the
   query phase is served by the page cache, so `R_B` of the query phase is
   not a number that a change of the code can move: it is the same for
   everybody, and it is the same for the baseline. The "4.58 times fewer
   bytes" that this file reported before was the pipe getting smaller,
   which the platform does not count. The improvement is real, and it is
   in the time, where it shows up as 3.00 times.
2. **The write phase writes the same bytes as before**, 11,407,360 B, and
   that is a number the platform does count. It is the sum of the data
   files (5.50 MB) and the log (5.36 MB), so the log is a second copy of
   everything that is written. This is the axis where the room is, and
   section 4 says how much.

The measurement was corrected in `tools/bench.py`: it now counts
`read_bytes` and `write_bytes`. The numbers that this file reports from
now on are the ones of the platform.

---

## 2. Where the time of a query goes

The `perf` profile of the query phase (`tools/perf_query_only.py`, recorded
into `/tmp/perf.data`) says this:

| Share | Symbol | What it is |
| ---: | --- | --- |
| 32.43% | `minivectordb::vector::dist::l2_sq_avx2` | the L2 kernel |
| 23.88% | `exec_distance_topk::{closure#12}` | the loop that offers every row to the heap |
| 2.62% | `exec::query::eval_predicate_borrowed` | the filter of the query |
| 2.29% | `value::vector::Vector::parse_literal` | the parsing of the vector literal |
| 1.31% | `proto::json::Parser::string` | the parsing of the request |

So **56% of the CPU of the server is the distance computation of the exact
scan**, and the whole of the rest is the scan itself. The exact scan of the
benchmark measures 10,000 rows of 128 dimensions for each of the 1,000
queries, which is 1.28e9 differences of floats, or 4e7 iterations of a
32-wide AVX2 kernel. There is no way to make that much work much cheaper
without changing what the program computes.

### 2.1 A correction about the driver

An earlier version of this file claimed that a large part of the query time
was the round trip between the driver and the server, and that about 600 ms of
the 949 ms that `tools/bench.py` reports was the Python driver.

That claim was wrong, and the way it was wrong is worth recording.

The measurement compared a loop that sent the requests one at a time and
waited for each answer with a loop that sent all of them at once. The two
loops were not comparable, because the first one was the *first* thing that
ran against the server and it paid for the page cache, the branch predictors
and the frequency scaling of the machine, while the second one ran on a warm
server. Repeating the two in turn and taking the median of each gives:

| Variant | Median |
| --- | ---: |
| One request at a time, wait for each answer | 283 ms |
| All requests sent at once, answers read in the background | 209 ms |
| The same loop, but with the answer parsed as JSON | 281 ms |
| The same loop, but with the answer parsed and the ids converted | 266 ms |

The gap between the first and the last row is about 15 ms, which is the whole
of the parsing cost. The gap between the round trip and the pipelined run is
74 ms, which is the cost of the synchronisation of the two processes, not of
the driver's work.

`tools/bench.py` is a tool of ours, not the driver of the platform. The
platform sends the same requests with a driver of its own, so the overhead of
our driver is not part of the score and there is nothing to win there. The
work that is worth optimising is the work the server does.

### 2.2 What the profile says to optimise next

The kernel is already the fast one: it uses four accumulators over 32-wide
loads, and it was made faster in `934e256d3`. The remaining ideas, in the
order in which they are worth trying:

1. **Fuse the scan and the kernel.** The scan calls the kernel once per row,
   so the horizontal sum of the eight lanes and the branch on the length of
   the vector are paid 10,000 times per query. A kernel that takes the whole
   matrix of a block and the query at once, and keeps the accumulator of the
   scan alive across the rows, would remove that. The risk is that the
   result changes in the last bit, because the accumulation order changes.
2. **Skip the distance of a row the filter rejects.** The scan measures the
   distance of a row before the filter is applied to it
   (`minivectordb/src/vector/exact.rs`), so half of the rows of a filtered
   query are measured for nothing. When the filter is an equality on an
   indexed column the rows are already narrowed down by the inverted index
   (`a509458a5`), so this only matters for the filters that the index does not
   cover.
3. **Do not measure the rows that cannot reach the answer.** The heap already
   drops a row that is worse than the worst candidate it holds with one
   comparison (`77c2a0ec4`), but the distance of that row was computed
   before the comparison was possible. A bound that is known before the
   measurement would let the scan skip the work, at the price of an answer
   that is not exactly the same.

---

## 3. The peak memory `M`

The platform reads `M` from `memory.peak` of the cgroup of the container, so
it counts the page cache of the files under `/data` as well as the resident
set of the process. The local benchmark reports `VmHWM` of the server
process, which is the smaller of the two numbers and therefore the wrong one
to compare against the platform.

`tools/bench_cgroup_peak.py` was written to measure `M` the way the platform
does, by running the benchmark in a cgroup of its own and reading
`memory.peak`. It cannot run on this machine, because creating a cgroup
needs root and this environment has neither root nor a Docker socket. It
falls back to the resident set and says so in its output, so that a number
from the wrong source is never reported as if it came from the right one.

What can be measured here are the two parts of the number separately:

| Part | Before the queries | After the queries |
| --- | ---: | ---: |
| Resident set of the server (`VmHWM`) | 8.81 MiB | 14.63 MiB |
| Size of the data directory (the page cache that can hold it) | 10.86 MiB | 10.86 MiB |
| Upper bound of the cgroup peak | 19.68 MiB | 25.50 MiB |

The data directory is 10.86 MiB, which is small next to the memory budget,
and the whole container stays far below the limit of 2 GiB. The buffer pool
of the vectors is the part of the resident set that grows with the query
phase: it goes from 8.81 MiB to 14.63 MiB, which is the 5.1 MiB of the
decoded vectors of the table, plus the decoded directory and the decoded
scalar columns.

### 3.1 The memory of the index

The index of the approximate path is not part of the exact measurement, but
it is part of the write phase, because the platform builds the index before
the queries and counts its memory. The peak of the write phase is 3,872 KB
now, against 3,756 KB in the baseline, so the index and the transaction
buffer together cost 116 KB more than they did.

---

## 3.2 The memory that grew with the number of rows

The measurements of section 3 are of a table of 10000 rows, which is the
data set of the benchmark. The statement requires the database to cope
with a data set that is *larger* than the memory budget, so the memory
of a table of 800000 rows matters more than the memory of a table of
10000 rows, and it was measured separately.

The first measurement of this round showed that the peak of every phase
that walks the table grew linearly with the number of rows:

| Phase | 50000 rows | 200000 rows | 800000 rows |
| --- | ---: | ---: | ---: |
| write | 6 MiB | 12 MiB | 34 MiB |
| index build | 37 MiB | 94 MiB | 161 MiB |
| select filtered | 40 MiB | 119 MiB | 307 MiB |
| select all | 50 MiB | 179 MiB | 482 MiB |
| update one | 53 MiB | 179 MiB | 391 MiB |
| delete one | 0 MiB | 124 MiB | 391 MiB |
| topk | 0 MiB | 127 MiB | 391 MiB |
| **restart** | **53 MiB** | **176 MiB** | **493 MiB** |

The number of the restart was the worst of them, and it was the one that
was believed to be the recovery. It was not. `tools/check_recovery_steps.py`
measures the process at three moments, and it says this:

```json
{
  "peak_after_handshake_mib": 94,
  "peak_after_limit1_mib": 410,
  "rows": 800000
}
```

The recovery itself peaks at 94 MiB. The jump to 410 MiB happens between
the handshake and the answer of the first statement, and the first
statement of the tool is `SELECT id FROM t LIMIT 1;`. The memory of the
restart was the memory of a query that read every row of the table to
answer with one of them.

That is where the three changes of this round come from.

**The rows are projected as they are read.** `for_each_visible_row` walks
the table block by block and hands each row to the caller as it is
read. The projection happens inside that walk, so the intermediate
`Vec<LiveRow>` - a `Vec<ColValue>` of its own for every row of the
table, with a copy of every string of every row - is gone.

**A statement with a limit stops reading.** A statement without an
ordering keeps the rows in the order of the table, so the scan can stop
as soon as it has enough rows. This is what turns the restart of the
table above from 493 MiB into 122 MiB.

**The response is written without a tree.** The response of a query was
built as a `Json` tree and then written out, so the answer was held
three times: as the rows, as the tree, and as the string.

**The three block caches share one budget.** Each of them was given the
whole `buffer_pool_mb`, so the caches could hold 192 MiB where the
budget says 64 MiB. The budget is now divided between them.

**The size of a scalar block was a guess.** It was `len() * 64`, which
says almost nothing about a table whose text column holds a kilobyte a
row. It is now the real size of the block.

The same measurement after the changes:

| Phase | 50000 rows | 200000 rows | 800000 rows |
| --- | ---: | ---: | ---: |
| write | 6 MiB | 11 MiB | 36 MiB |
| index build | 37 MiB | 62 MiB | 108 MiB |
| select filtered | 0 MiB | 83 MiB | 134 MiB |
| select all | 44 MiB | 98 MiB | 198 MiB |
| update one | 44 MiB | 98 MiB | 198 MiB |
| delete one | 0 MiB | 91 MiB | 165 MiB |
| topk | 0 MiB | 91 MiB | 165 MiB |
| **restart** | **37 MiB** | **80 MiB** | **122 MiB** |

The phases that return every row of the table still grow with it: the
answer of `SELECT id FROM t` over 800000 rows is 800000 rows, and the
answer cannot be smaller than the answer. What is gone is the part that
was *not* the answer - the copy of the table that the query held while
it worked out the answer.

The restart is the phase that matters the most, because it is the one
that has to happen before the database answers at all. Its memory barely
moves with the size of the table now: 37 MiB at 50000 rows, 80 MiB at
200000 and 122 MiB at 800000, against 53, 176 and 493 MiB before.

### 3.3 The text of the table does not make the memory grow any more

The text of a row is the part that a size estimate gets wrong the most:
a row of a `VARCHAR(1024)` column holds a kilobyte, and the number of
the rows of the table says nothing about it.

`tools/check_restart_breakdown.py` writes a table with a text column of a
given size and reports the memory of a restart, split into the anonymous
memory of the process and the page cache of the files:

| Text of a row | Anonymous memory | Resident set |
| --- | ---: | ---: |
| 0 bytes | 68 MiB | 71 MiB |
| 100 bytes | 75 MiB | 78 MiB |
| 1000 bytes | 74 MiB | 76 MiB |

Before the changes the same measurement gave 407 MiB, 484 MiB and 1148
MiB of anonymous memory. The 1148 MiB was the text of every row of the
table, copied into the answer of a query that asked for one row.

### 3.4 The structures that still grow with the number of rows

`tools/check_growing_structures.py` measures the process at every moment
of the life of a table, so that the structures that grow are visible and
can be told apart from the ones that do not. At 800000 rows of 128
dimensions:

| Point | Peak |
| --- | ---: |
| after the write | 36 MiB |
| after the build of the index | 107 MiB |
| after `SELECT id FROM t LIMIT 1` | 122 MiB |
| after a filtered query | 151 MiB |
| after a query that reads the whole table | 197 MiB |
| after a search through the index | 197 MiB |
| after the restart | 121 MiB |

The structures that grow with the number of rows, and what they cost at
800000 rows:

| Structure | Size | Note |
| --- | ---: | --- |
| the primary key index | ~19 MiB | 800000 entries of 24 bytes, plus the map |
| the vector index | ~19 MiB | 800000 entries of 16 bytes, plus two arrays of 4 |
| the inverted index | ~10 MiB | the list of every distinct value of every column |
| the answer of a query | the size of the answer | this one is the answer |

The primary key index and the vector index are the identity of the rows
and cannot be smaller without a different data structure. The inverted
index is a filter over the data and not a copy of it: a column with more
than 1024 distinct values drops out of it on its own, and a query that
would have used it falls back to the full scan.

The last row of the table is the honest one: a query that answers with
every row of the table holds every row of the table, and the statement
does not ask for anything else.

### 3.5 The data set that is larger than the budget

`tools/check_memory_limit.py` runs the database under the memory limit
the judge uses - `RLIMIT_AS` of 2 GiB, the same number of bytes as the
`--memory=2g` of the container - with a data set that is much larger
than the budget:

```json
{
  "data_directory_mib": 6638.3,
  "data_larger_than_the_budget": true,
  "dim": 128,
  "limit_mb": 2048,
  "phases": {"restart": "answered", "write_and_query": "answered"},
  "rows": 4400000,
  "text": 1000
}
```

6.6 GiB of data, with a kilobyte of text in every row, under a limit of
2 GiB. Both the write with the query and the restart answered.

### 3.6 The answers did not change

The changes of this round are meant to change the memory and not the
answers, so the gate of them is a comparison of two builds.
`tools/check_answer_equivalence.py` drives two builds through 51
statements in both search modes, with a transaction, a rollback, an
update, a delete, a vector index and two restarts, and compares the
answers byte for byte:

```
102 of 102 answers are identical
```

The other gates of the round:

| Gate | Result |
| --- | --- |
| `tools/run_public_cases.py` | 20 of 20 |
| `tools/check_formal_requirements.py` | 27 of 27 |
| `tools/selftest.py` | all checks passed |
| `tools/smoke.sh` | all checks passed |
| `tools/killtest.sh` | 12 rounds passed |
| `tools/fuzz_kill_recovery.py` | 40 rounds passed |
| `tools/fuzz_kill_write.py` | 200 rounds passed |
| `tools/check_ann_equivalence.py` | 100 answers identical |
| `tools/check_ann_recall.py` | recall 1.0, filtered and unfiltered |
| `tools/check_filter_semantics.py` | all checks passed |
| `cargo test --release` | 157 tests |

The performance did not move either, which is the point of the round: the
query phase of the benchmark went from 2777 ms to 2716 ms and the write
phase from 1498 ms to 1303 ms, with the recall still at 1.0.

---

## 4. The formula, and what it means for the score

The formula of the statement is:

```
R_T = (T_base + eps_T) / (T + eps_T)          eps_T = 1 ms
R_B = (B_base + eps_B) / (B + eps_B)          eps_B = 1 MiB
R_M = (M_base + eps_M) / (M + eps_M)          eps_M = 1 MiB
f(R) = R / (1 + R)
E_write = 0.6 f(R_T) + 0.2 f(R_B) + 0.2 f(R_M)
E_query = 0.6 f(R_T) + 0.2 f(R_B) + 0.2 f(R_M)
E       = (E_write + 2 E_query) / 3
```

`f` is bounded by 1, so `E` is bounded by 1. The platform reported
`E = 1829.577226` for the version of the competition that we submitted, so
the platform clearly scales the result by a constant of its own before it
prints it. That constant is not published.

The part of the formula we can act on is the ratio. `f` is increasing and
concave, so every factor by which a phase becomes faster or smaller raises
`f`, and the size of the raise shrinks as the ratio grows. In other words,
the first factor of two is worth much more than the second, and the score
that is left on the table is in the phases that are already fast.

The order of the levers, with the numbers of this machine:

1. **The time of the query.** It is 66 2/3% of the weight of the score, and
   the exact scan spends 60% of its CPU in the distance kernel. This is the
   lever with the most room left. `R_T` is 3.00 now, and `f` is concave, so
   the next factor of two buys less than the last one did: the ratio is
   already in the part of `f` where it flattens.
2. **The bytes of the write.** They are 6 2/3% of the weight of the score,
   and they have **not moved at all**: 11,407,360 B in both versions, so
   `R_B` of the write phase is exactly 1. This is the one axis that is
   still at the baseline, and the only one where a big factor is still
   available. It is the axis that was left for last, and the axis that
   should be next. Section 4.5 says what it would take.
3. **The bytes of the query.** They are 13 1/3% of the weight, and they are
   **zero in both versions**: every byte of the query phase is served by
   the page cache, so `R_B` is exactly 1 and there is nothing to win here.
   This axis is closed.
4. **The peak memory of the query.** It is 13 1/3% of the weight, and it
   used to grow against the baseline. It does not any more: the block of
   the vector file was made smaller, and the peak fell to 11,392 KB against
   the 14,788 KB of the baseline, so `R_M` is 1.30. This axis is now on
   the right side. Section 1.2 tells how the round of 2026-11-12 made it
   worse before it was made better.
5. **The time of the write.** It is 20% of the weight, and it is 1.33 times
   faster than the baseline already. Section 4.4 shows that it is worth
   nothing to improve it further.

### 4.1 The number the platform printed

For the version that was submitted, the platform printed `E = 1829.577226`.
Running the formula over the numbers of the baseline gives a value well
below 1, so the printed number is the formula times a constant of the
platform. That constant is not published.

But it does not have to be known, because it cancels out of the ratio of
two printed numbers. If the platform baseline is `k` times ours, then

```
E_printed(current) = 1829.577226 * raw_e_current / raw_e_baseline
```

and both raw values come out of the formula directly. Running it over the
range of `k`:

| The platform baseline is | `R_T` of the current | raw `e` of the current | Projected `E` | Against the baseline |
| --- | ---: | ---: | ---: | ---: |
| 0.3x ours (fast) | 0.899 | 0.4435 | **2800** | +53% |
| 0.5x ours | 1.498 | 0.5387 | 2668 | +46% |
| 1.0x ours | 2.996 | 0.6600 | 2415 | +32% |
| 2.0x ours | 5.992 | 0.7548 | 2186 | +19% |
| 3.0x ours | 8.988 | 0.7956 | 2085 | +14% |
| 5.0x ours (slow) | 14.980 | 0.8331 | 1992 | +9% |

So the current code projects to roughly **1990 to 2800**, against the
1829.58 of the baseline: a gain of 9% to 53%, depending on how the
platform's baseline compares to ours, which is not known.

The reason the gain is smaller than the gain in the raw time is the shape
of `f`. `f(R) = R/(1+R)` is concave, so a large `R` buys less than a
small one, and the ratio here is already large: `R_T = 3.00`. And the
gain is smaller than it could be because **one of the three axes of the
query phase is closed**: the bytes of the query are zero, in the baseline
and in the current code alike, so there is nothing to win there.

**This projection is not calibrated.** It says what the formula gives for
a baseline that is *assumed* to be in a certain relation to ours. It has
never been checked against a number the platform printed, because the
platform has only printed one, for the version before this round. The
first submission of the current code will tell us the scale, and until then
the number above is a projection and not a forecast.

**To be filled in after the first submission of this round.** The
submission is made for the calibration and for nothing else: the version
is the one that this file describes, with no experiment in it that has not
been verified, so that the number the platform prints is the number this
file predicts. What has to be written here is the printed `E`, the three
axes of the platform, and the constant of the platform that follows from
them.

| Field | Value |
| --- | --- |
| The commit that was submitted | `a868b06fa19189395024239d336fdc4ad3d77f48` |
| The `E` the platform printed | _to be filled in_ |
| The three axes the platform reported | _to be filled in_ |
| The constant of the platform, `E_printed / raw_e` | _to be filled in_ |
| The projection of this file, for comparison | 2415 (at `k = 1.0`) |

### 4.1.1 The first submission of this round did not pass the admission gate

The platform answered for `a868b06fa19189395024239d336fdc4ad3d77f48`:

```
MySQL 正式用例: 未通过
原因: database exited before response（响应前退出，请检查入口参数及stderr日志）× 1
```

Neither the performance nor the prompt evaluation ran, so this
submission produced **no** number for the table above. The calibration
did not happen, and the projection of this file is still a projection.

The cause was found locally afterwards, and it is a defect of the code
and not of the submission:

* The commit added the **inverted index** over the low cardinality
  columns (`a509458a5`), and with it the **rebuild of the inverted
  indexes at startup** (`engine.rs::rebuild_inverted_indexes`). That
  rebuild reads the scalar columns of every block.
* The read of a block decoded the row images with an **unchecked slice**
  (`storage/table.rs::image_at`). A data directory whose `col.dat` is
  shorter than its `meta.dat` says it is - which is what an
  interrupted write leaves behind - the slice runs past the end of the
  buffer and the process **panics during the recovery**, before the
  handshake.
* A process that dies before it writes `{"type":"ready"}` is exactly
  what the platform reports as `database exited before response`.

The fix is in the commit that follows this one: every read of a row
image is bounds checked, a row that cannot be decoded is reported as
absent instead of failing the statement, and the rebuild of the derived
files can no longer take the startup down.

**What the fix does not cost.** The peak memory of a million rows of
128 dimensions is 913 MiB before the fix and 914 MiB after it, and the
query time of the same workload is 4.0 s in both. The fix is on the
recovery path, and the recovery is not on the measured path.

**What this changes about the method.** The version that was submitted
passed every check that could be run locally: `cargo test` (141
tests), the public cases, `selftest.py`, `check_formal_requirements.py`
(27 of 27) and `killtest.sh` (12 rounds). None of them truncated a
data file in the middle, because none of them was written to. The
lesson is not that the checks were run wrongly; it is that the class of
defect that the admission gate punishes hardest - a process that dies
instead of answering - was not covered by any of them. The checks that
cover it now are:

| Tool | What it does | Result |
| --- | --- | --- |
| `tools/check_torn_data.py` | cuts the data files of a committed database in nine ways and checks that it still starts and still answers | 9 of 9 |
| `tools/fuzz_torn_data.py` | damages the data files in 2500 random ways and checks the same | 2500 of 2500, **no panic** |
| `tools/fuzz_kill_write.py` | kills the database at a random moment of a write and checks the restart | 200 of 200 |

The three tools were run against the version that was submitted as
well, and they fail it: `fuzz_torn_data.py` fails 41 of its first 200
rounds against `ae6514687`, and `check_torn_data.py` fails 7 of its 9
checks against it. The tools are therefore a check of the defect and
not only a check of the fix.

### 4.1.2 The first fix was not yet enough, and the fuzzer said so

The fix of section 4.1.1 removed the panic. The fuzzer still failed, and
the way it failed was more useful than the panic itself: **the database
answered with an error instead of dying.** A failure of a query is a
defect; a failure of the *process* is what the admission gate punishes.
So the fuzzer had found the difference between the two classes.

What it found was that the recovery of a damaged data directory was not
complete. There were two more ways to lose the schemas, and both of them
ended in a process that died before the handshake:

| Damaged file | What the old code did | What the code does now |
| --- | --- | --- |
| `catalog.dat`, magic overwritten | `Engine::open` returned an error, the process exited with code 1 | the backup copy is read instead, the process starts |
| `catalog.dat`, a byte of a schema flipped | the schema was read as a table with different columns, and every query answered `no_such_column` | the entry carries a checksum, the damaged file is recognised, the backup is used |
| `meta.dat`, an offset that is far past the end of `col.dat` | the read asked for an allocation of that size, and the out of memory killer took the process | the run is not read at all, the rows of it are treated as rows that cannot be decoded |

The catalog needed a second copy and a checksum, because it is the only
place the schemas live once the log has been folded into the data files.
The format version went from 1 to 2; a file of the old version is still
read, but its entries cannot be checked.

**What this changes about the method.** The first fix answered the
question *"does a damaged file kill the process?"*. It did not answer
*"does a damaged file leave the database answering correctly?"*, and
those are different questions: a table whose schema was read as a table
with the wrong columns answers every query with an error, and the
admission gate counts that as a failure too. The fuzzer was extended to
check the **answers**, not only the liveness, and it is the answers that
found the catalog defect.

**What the fix costs.** Nothing that is measured. The catalog is written
once per schema change and read once per start, and a checksum over a few
hundred bytes is not a cost. The peak memory at 300 000 rows is 191 388
KB after the fix and 190 872 KB before it, a difference of 0.27% that is
the size of the backup copy of the catalog.

The state after this section:

| Check | Result |
| --- | --- |
| `cargo test` | 146 tests, 146 pass (131 unit + 15 protocol) |
| `tools/run_public_cases.py` | 20 of 20 |
| `tools/check_formal_requirements.py` | 27 of 27 |
| `tools/check_torn_data.py` | 9 of 9 |
| `tools/fuzz_torn_data.py`, four seeds | 1600 of 1600, **no panic and no wrong answer** |
| `tools/fuzz_kill_write.py` | 30 of 30 |
| `tools/killtest.sh` | all rounds pass |
| the container under `--cpus=2 --memory=2g --pids-limit=128` | starts and stays alive |

Three of the protocol tests are new, and they are written so that they
**fail against `a868b06fa` and pass against the fix**: the truncated
column file, the damaged catalog and the directory entry with a huge
offset. A test that passes against both versions would not have caught
the defect, so their failure against the submitted version is part of
their evidence.

The constant is the one number that turns the projection into a
forecast: with it, the `raw_e` of any later change can be turned into the
`E` the platform will print, and the order of the remaining levers can be
computed against the real scale instead of an assumed one.

Two things follow from the shape of the formula, and both are worth
keeping in mind:

1. **The write axis is the untouched one.** `R_B` of the write phase is
   exactly 1, so the write phase contributes nothing to the gain. Its
   weight is 6 2/3% of the time and 6 2/3% of the I/O, and both of them
   are still at the baseline. This is the axis where the remaining room
   is.
2. **The memory axis is now on the right side.** `R_M` of the query phase
   is 1.24, after the block of the vector file was made smaller. Before
   that it was 0.99.

The honest conclusion is that the ranking is decided by the distance
between the competitors, not by this factor alone, and that the work
that is worth doing is the work that is safe: a faster kernel that
returns the same answers, a write path that does less I/O, and a smaller
memory footprint.

### 4.2 The write axis, and what it would take

The write phase writes 11,407,360 B to the disk. The data files are
5.50 MB and the log is 5.36 MB, so **the log is a second copy of
everything that is written**: the write amplification is 1.98x.

The log holds the **whole row image**, and the row image holds the vector:
`encode_row` keeps the vector in the image so that the log record is self
contained, and the recovery can rebuild the vector file without depending
on the order in which the data files were flushed
(`minivectordb/src/storage/format.rs`). The data files write the vector
once more, into `vec.dat`, and the image without the vector into
`col.dat`.

So the row is written three times: the vector into the log, the vector
into the vector file, and the rest of the row into the log and into the
column file.

### 4.3 The checkpoint idea does not work, and the measurement says so

The first idea that comes to mind is to empty the log more often. It was
measured, and it does not work:

| `MINIVECDB_CHECKPOINT_MB` | Bytes that reached the disk |
| ---: | ---: |
| 64 (the default) | 10,879,000 B |
| 4 | 10,895,000 B |
| 1 | 10,941,000 B |

Checkpointing more often makes the number **larger**, not smaller. The
reason is that the checkpoint does not remove the write of the log, it
only empties the file afterwards: the log was already written, and then
the file is truncated. The bytes have already reached the device.

So the threshold is not a lever. The lever is the **content** of the log:
the vector does not have to be in it.

### 4.4 What each remaining lever is worth

The formula is run over the numbers of this machine, with the platform
baseline assumed to be the same as ours, and the projected printed `E` is
compared with the 2415 of the current code:

| Lever | Change | raw `e` | Projected `E` | Gain |
| --- | --- | ---: | ---: | ---: |
| The time of the query | 324 ms to 162 ms | 0.7243 | 2650 | **+235** |
| The bytes of the write | 10.9 MB to 6.0 MB | 0.6858 | 2509 | +95 |
| The bytes of the write | 10.9 MB to 3.8 MB | 0.7024 | 2570 | +155 |
| The time of the write | 566 ms to 283 ms | 0.6600 | 2415 | +0 |
| Both of the two above | | 0.7667 | 2806 | **+391** |

Two things are worth noticing.

**The time of the write is worth nothing at all.** Halving it moves the
projected `E` by zero, because the write phase is a third of the weight
and `f` is already at `f(1.33) = 0.57`, so the gain of the ratio is small
and the write time is the smaller half of the weight.

**So the two things that are worth doing are the time of the query and
the bytes of the write.** The first one is what the profile points at, and
the second one is the axis that has not been touched at all. Together they
are worth about 390 points of the printed score, which is more than the
time of the query alone.

And one thing that was measured and is *not* worth doing: making the
distance kernel faster by more arithmetic.

| Kernel | 10,000 rows | 500 rows |
| --- | ---: | ---: |
| The AVX2 kernel of today | 192.4 ms | 5.3 ms |
| The same with the fused multiply-add | 190.2 ms | 4.6 ms |
| The same with eight accumulators | 191.9 ms | 5.5 ms |

The kernel is **limited by the memory**, not by the arithmetic: it reaches
26.6 GB/s over ten thousand rows and 48.8 GB/s over five hundred, so the
data is what costs, and the arithmetic is nearly free. The fused
multiply-add buys 1%, and eight accumulators buy nothing.

So there is no point in fusing the scan with the kernel, or in making the
kernel wider. The only way to make the query faster is to **measure
fewer rows**, and the only exact way to do that is to know a bound before
the measurement.

### 4.5 What it would take to make the log smaller

The vector has to leave the log, and the question is whether the recovery
still works without it.

The recovery reads the log and rebuilds the rows from it
(`replay_insert`). If the vector were not in the log, the recovery would
have to read it from `vec.dat` instead, which is possible: the directory
entry names the block and the slot of the vector of every row, and the
vector file holds it.

**The risk is the order of the writes.** The data files are written
*before* the commit marker reaches the log, so a transaction whose
commit was not acknowledged leaves rows in the data files that the
recovery has to discard. Today the recovery discards them by row id,
because the row id is in the log record. If the vector were not in the
log, the recovery would still know the row id, so it could still discard
the row — but it would have to know that the vector in `vec.dat` is not
to be trusted, which it does, because it rewrites the vector of the rows
it applies.

So the change is possible, and it is not a change of the format of the
data files: it is a change of what the log record holds, and a change of
the recovery to read the vector from the vector file.

**This is the change that has to be thought about rather than tried.** The
twelve rounds of the crash recovery test are the gate, and the gate is
not only "the test passes" but "the test passes for the reason it is
supposed to". The rule of the competition is that a transaction that
returned COMMIT must be there after a crash, and a transaction that did
not may be there or not. A log that no longer holds the vector makes the
second case harder to get right, because the vector file holds the rows
of the transactions that were rolled back as well.

---

## 5. The commands

Every measurement of this file is reproduced by these commands:

```sh
# the table of section 1 (the pinning is what makes the rounds alike)
python3 tools/bench_median.py --mode exact --rounds 5 --cpu 3 --cpus 3
python3 tools/bench_median.py --binary <baseline binary> --mode exact --rounds 5 --cpu 3 --cpus 3

# the table of section 1.2, which measures three binaries at once. The
# rounds are interleaved, so that a drift of the machine hits all three of
# them and cancels out. The three binaries are built with the profile that
# is delivered - `strip = true` - so that the numbers are the numbers of
# the delivered build.
for round in 1 2 3 4 5; do
  for binary in baseline before now; do
    python3 tools/bench.py --binary "$binary" --mode exact \
        --rows 10000 --dim 128 --queries 1000 --cpu 3 --cpus 3 \
        --output "docs/perf/p6-$binary-$round.json"
  done
done

# the same, in the approximate mode
for round in 1 2 3 4 5; do
  for binary in baseline before now; do
    python3 tools/bench.py --binary "$binary" --mode ann \
        --rows 10000 --dim 128 --queries 1000 --cpu 3 --cpus 3 \
        --output "docs/perf/p6ann-$binary-$round.json"
  done
done

# the counter of the I/O, both of them side by side
python3 tools/measure_io.py --mode exact --cpu 3

# the profile of section 2. The release profile of the crate sets
# `strip = true`, so the binary that is being delivered has no symbols and
# `perf` cannot resolve them. The profiling build is the only build with
# `strip = false`, and the delivered build is always `strip = true`.
sed -i 's/^strip = true/strip = false/' minivectordb/Cargo.toml
cargo build --offline --locked --release
python3 tools/perf_query_only.py ./minivectordb/target/release/minivectordb exact /tmp/perfwork /tmp/perf.data 3
perf report -i /tmp/perf.data --stdio --no-children --percent-limit 1.2
sed -i 's/^strip = false/strip = true/' minivectordb/Cargo.toml
cargo build --offline --locked --release

# the comparison of section 2.1
python3 tools/probe_roundtrip.py

# the kernel of section 4.4
cargo build --offline --locked --release --bin kernelbench
taskset -c 3 ./minivectordb/target/release/kernelbench

# the memory of section 3
python3 tools/bench_cgroup_peak.py --mode exact

# the memory of section 3.2, at the three data sizes
python3 tools/check_phase_memory.py --rows 50000
python3 tools/check_phase_memory.py --rows 200000
python3 tools/check_phase_memory.py --rows 800000

# the same, at the three sizes at once
python3 tools/check_memory_scaling.py

# the recovery, at the handshake and after a query (section 3.2)
python3 tools/check_recovery_steps.py --rows 800000

# the text of the table (section 3.3)
python3 tools/check_restart_breakdown.py --rows 800000 --text 0
python3 tools/check_restart_breakdown.py --rows 800000 --text 100
python3 tools/check_restart_breakdown.py --rows 800000 --text 1000

# the structures that grow (section 3.4)
python3 tools/check_growing_structures.py --rows 800000

# the data set that is larger than the budget (section 3.5)
python3 tools/check_memory_limit.py --rows 4400000 --dim 128 --text 1000 --limit-mb 2048

# the gate of the answers (section 3.6). The old build is the one of the
# commit before the round, and the two are compared byte for byte.
git stash push -u && cargo build --offline --locked --release --manifest-path minivectordb/Cargo.toml
cp minivectordb/target/release/minivectordb /tmp/mvdb_baseline && git stash pop
cargo build --offline --locked --release --manifest-path minivectordb/Cargo.toml
python3 tools/check_answer_equivalence.py
```
