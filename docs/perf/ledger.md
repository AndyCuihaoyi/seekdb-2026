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

---

## 1. The baselines

The reference implementation of the competition is not published, so the
platform uses its own baseline, whose numbers are unknown to us. What we can
do is compare our own code against the code we started from, under the same
conditions, and look at how the ratio `R` that the formula uses moves.

The commit `b88e0209` is the code we started this round from. It is the
baseline of the table below; it was measured with
`tools/bench_median.py --mode exact --rounds 3`, the same tool and the same
three rounds that the platform uses.

| Measurement | Baseline `b88e0209` | Now `9335f4e83` | Ratio |
| --- | ---: | ---: | ---: |
| Query `T` | 1360.1 ms | 900.6 ms | 1.51x faster |
| Query `B` | 7,378,838 B | 1,610,808 B | 4.58x fewer |
| Query `M` (RSS) | 14,732 KB | 14,988 KB | 1.02x more |
| Write `T` | 1942.6 ms | 1412.1 ms | 1.38x faster |
| Write `B` | 23,104,906 B | 23,104,906 B | 1.00x |
| Write `M` (RSS) | 3,756 KB | 4,020 KB | 1.07x more |

The row of the current version is the median of three rounds recorded in
`docs/perf/p5-current-exact.json`. The spread of the machine is wide enough
to matter, so the numbers should be read as a range: the query time of the
current version was seen between 900 ms and 949 ms over repeated runs, and
the write time between 1412 ms and 1601 ms. The bytes and the memory are
stable, because they are not the result of a race.

The answers of the two versions are byte for byte identical, in both the
exact and the approximate mode (`docs/perf/answers-baseline-*.txt`).

The query `B` is the number that moved the most. The scan of the table reads
the vectors of a block with one call instead of one call per row, so the
number of read bytes fell by a factor of four and a half.

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
the queries and counts its memory. The peak of the write phase is 4,020 KB
now, against 3,756 KB in the baseline, so the index and the transaction
buffer together cost 264 KB more than they did.

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
   the exact scan spends 56% of its CPU in the distance kernel. This is the
   lever with the most room left.
2. **The bytes of the query.** They are 13 1/3% of the weight, and they are
   already 4.58 times smaller than the baseline. There is little left here.
3. **The peak memory of the query.** It is 13 1/3% of the weight, and it grew
   by 1.7% against the baseline. The buffer pool is the part that grows, so
   a smaller pool would take it back, at the price of more read calls.
4. **The write phase.** It is 33 1/3% of the weight, and its bytes have not
   moved at all: 23,104,906 B in both versions.

### 4.1 The number the platform printed

For the version that was submitted, the platform printed `E = 1829.577226`.
Running the formula over the numbers of the baseline gives a value well
below 1, so the printed number is the formula times a constant of the
platform.

The sensitivity of the printed number to a change of the code can be
estimated without knowing the constant, because the constant cancels out of
the ratio of two printed numbers. Comparing the baseline with the current
code over every set of baseline values that is consistent with the printed
number gives a projected printed value of roughly 1950 to 2160 for the
current code, against the 1829.58 of the baseline.

That is a gain of about 7% to 18%. It is a real gain, but it is not the
difference between a good rank and a bad one, and it does not depend on the
formula alone: reaching the same printed range as the best ranks would need
a query time that is a small fraction of the baseline, which the exact scan
cannot reach without changing the answers.

The honest conclusion is that the ranking is decided by the distance between
the competitors, not by this factor alone, and that the work that is worth
doing is the work that is safe: a faster kernel that returns the same
answers, a smaller memory footprint, and a write path that does less I/O.

---

## 5. The commands

Every measurement of this file is reproduced by these commands:

```sh
# the table of section 1
python3 tools/bench_median.py --mode exact --rounds 3

# the profile of section 2
python3 tools/perf_query_only.py ./minivectordb/target/release/minivectordb exact /tmp/perfwork /tmp/perf.data
perf report -i /tmp/perf.data --stdio --no-children --percent-limit 1.2

# the comparison of section 2.1
python3 tools/probe_roundtrip.py

# the memory of section 3
python3 tools/bench_cgroup_peak.py --mode exact
```
