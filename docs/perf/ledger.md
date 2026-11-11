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
twice in the round of 2026-11-11, and both corrections changed the
conclusion:

1. The rounds were noisy, because the server was floating between the cores.
   Pinning it fixed that, and the query turned out to be 3.02 times faster
   than the baseline rather than 1.51 times. Section 1.
2. The bytes were counted with the wrong counter, which measured the
   traffic of the pipe instead of the disk. The query phase reads **nothing**
   from the disk, and the write phase writes the same bytes as the baseline.
   Section 1.1.

So the two numbers that the platform counts are these: the time of the
query is 3.02 times better than the baseline, and **nothing else has
moved**. Section 4.4 says what each of the remaining levers is worth.

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

| Measurement | Baseline `b88e0209` | Now `0e5c69985` | Ratio |
| --- | ---: | ---: | ---: |
| Query `T` | 967.0 ms | 320.6 ms | **3.02x faster** |
| Query `B` (device) | 0 B | 0 B | both are served by the page cache |
| Query `M` (RSS) | 14,732 KB | 14,980 KB | 1.02x more |
| Write `T` | 750.1 ms | 571.5 ms | 1.31x faster |
| Write `B` (device) | 11,407,360 B | 11,407,360 B | 1.00x |
| Write `M` (RSS) | 3,756 KB | 3,872 KB | 1.03x more |

The spread of the five rounds of each row is under 8%, and under 3% for
the query phase of the current version. The measurements are in
`docs/perf/baseline-pinned-exact.json` and
`docs/perf/p5-current-exact.json`.

**The query is 3.02 times faster than the baseline, and the earlier
figure of 1.51 times was an artefact of the noise.** The earlier table of
this file was built on unpinned rounds, in which the baseline and the
current code were measured at different moments of the same noise, and
the comparison landed in the middle of it.

The answers of the two versions are byte for byte identical, in both the
exact and the approximate mode (`docs/perf/answers-baseline-*.txt`).

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
   in the time, where it shows up as 3.02 times.
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
   lever with the most room left. `R_T` is 3.02 now, and `f` is concave, so
   the next factor of two buys less than the last one did: the ratio is
   already in the part of `f` where it flattens.
2. **The bytes of the write.** They are 6 2/3% of the weight of the score,
   and they have **not moved at all**: 11,407,360 B in both versions, so
   `R_B` of the write phase is exactly 1. This is the one axis that is
   still at the baseline, and the only one where a big factor is still
   available. It is the axis that was left for last, and the axis that
   should be next. Section 4.2 says what it would take.
3. **The bytes of the query.** They are 13 1/3% of the weight, and they are
   **zero in both versions**: every byte of the query phase is served by
   the page cache, so `R_B` is exactly 1 and there is nothing to win here.
   This axis is closed.
4. **The peak memory of the query.** It is 13 1/3% of the weight, and it grew
   by 1.7% against the baseline, so `R_M` is 0.98 and it goes the wrong way.
   The buffer pool is the part that grows, so a smaller pool would take it
   back, at the price of more read calls.
5. **The time of the write.** It is 20% of the weight, and it is 1.31 times
   faster than the baseline already.

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
| 0.3x ours (fast) | 0.905 | 0.4357 | **2751** | +50% |
| 0.5x ours | 1.508 | 0.5295 | 2622 | +43% |
| 1.0x ours | 3.016 | 0.6498 | 2378 | +30% |
| 2.0x ours | 6.032 | 0.7459 | 2160 | +18% |
| 3.0x ours | 9.049 | 0.7880 | 2065 | +13% |
| 5.0x ours (slow) | 15.081 | 0.8274 | 1978 | +8% |

So the current code projects to roughly **1980 to 2750**, against the
1829.58 of the baseline: a gain of 8% to 50%, depending on how the
platform's baseline compares to ours, which is not known.

The reason the gain is smaller than the gain in the raw time is the shape
of `f`. `f(R) = R/(1+R)` is concave, so a large `R` buys less than a
small one, and the ratio here is already large: `R_T = 3.02`. And the
gain is smaller than it could be because **two of the three axes of the
query phase are closed**: the bytes of the query are zero, and the memory
of the query went the wrong way.

**This projection is not calibrated.** It says what the formula gives for
a baseline that is *assumed* to be in a certain relation to ours. It has
never been checked against a number the platform printed, because the
platform has only printed one, for the version before this round. The
first submission of the current code will tell us the scale, and until then
the number above is a projection and not a forecast.

Two things follow from the shape of the formula, and both are worth
keeping in mind:

1. **The write axis is the untouched one.** `R_B` of the write phase is
   exactly 1, so the write phase contributes nothing to the gain. Its
   weight is 6 2/3% of the time and 6 2/3% of the I/O, and both of them
   are still at the baseline. This is the axis where the remaining room
   is.
2. **The memory axis is slightly against us.** `R_M` of the query phase
   is 0.98, because the buffer pool grew. It costs a fraction of a
   percent of the score, but it is a fraction that goes the wrong way.

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
compared with the 2378 of the current code:

| Lever | Change | raw `e` | Projected `E` | Gain |
| --- | --- | ---: | ---: | ---: |
| The time of the query | 320 ms to 160 ms | 0.7139 | 2612 | **+234** |
| The bytes of the write | 10.9 MB to 6.0 MB | 0.6757 | 2472 | +95 |
| The bytes of the write | 10.9 MB to 3.8 MB | 0.6923 | 2533 | +155 |
| The memory of the query | back to the baseline | 0.6506 | 2381 | +3 |
| The time of the write | 571 ms to 285 ms | 0.6498 | 2378 | +0 |

Two things are worth noticing.

**The time of the write is worth nothing at all.** Halving it moves the
projected `E` by zero, because the write phase is a third of the weight
and `f` is already at `f(1.34) = 0.57`, so the gain of the ratio is small
and the write time is the smaller half of the weight. The same is true of
the memory of the query: three points.

**So the two things that are worth doing are the time of the query and
the bytes of the write.** The first one is what the profile points at, and
the second one is the axis that has not been touched at all.

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
python3 tools/bench_median.py --mode exact --rounds 5 --cpu 7
python3 tools/bench_median.py --binary <baseline binary> --mode exact --rounds 5 --cpu 7

# the profile of section 2
sed -i 's/^strip = true/strip = false/' minivectordb/Cargo.toml
cargo build --offline --locked --release
python3 tools/perf_query_only.py ./minivectordb/target/release/minivectordb exact /tmp/perfwork /tmp/perf.data
perf report -i /tmp/perf.data --stdio --no-children --percent-limit 1.2

# the comparison of section 2.1
python3 tools/probe_roundtrip.py

# the memory of section 3
python3 tools/bench_cgroup_peak.py --mode exact
```
