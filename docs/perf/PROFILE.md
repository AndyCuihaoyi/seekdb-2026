# The profile of the query phase

This file is the anchor of the performance work: it says where the time of a
query goes, how that was measured, and what follows from it. It is written
to be read by whoever picks the work up next, without having to rerun
anything.

Everything here was measured on the code of this repository, with the
commands given in each section. The numbers of the previous round are kept
where they are useful, but every conclusion is about the code as it stands.

---

## 1. The workload

The benchmark of the competition is small and fixed:

| | |
|---|---|
| Rows | 10,000 |
| Dimension | 128 |
| Queries | 1,000, each a TopK with K = 10 |
| Filter | half of the queries carry an equality on a low-cardinality column |
| Mode | exact (the mode the Dockerfile freezes) |

The exact scan measures **every visible row** of the table for **every**
query. That is the arithmetic that decides everything else:

```
10,000 rows x 128 dimensions x 1,000 queries = 1.28e9 differences of floats
                                             = 4e7 iterations of a 32-wide AVX2 kernel
```

No amount of clever indexing removes that work, because the answer of an
exact search over ten thousand vectors is the answer over ten thousand
vectors. The only question is how much of the cost is the arithmetic and
how much is everything around it.

---

## 2. The measurement

### 2.1 The sampling profiler

```sh
# build without stripping, or the symbols will not resolve
sed -i 's/^strip = true/strip = false/' minivectordb/Cargo.toml
cargo build --offline --locked --release
python3 tools/perf_query_only.py ./minivectordb/target/release/minivectordb \
    exact /tmp/perfwork /tmp/perf.data
perf report -i /tmp/perf.data --stdio --no-children --percent-limit 1.0
```

**The release profile of this crate sets `strip = true`, so the binary has
no symbols and `perf` reports raw addresses.** A profile of a stripped
binary looks like a list of hexadecimal numbers and is easy to misread as
"the work is somewhere in the program". Turn the stripping off for the
build that is being profiled, and turn it back on for the build that is
being delivered.

The tool runs the whole 1,000 queries once to warm the caches, and then
records a second pass, so that the samples attribute the compute and not the
reading of the files.

### 2.2 The result

| Share | Symbol | What it is |
| ---: | --- | --- |
| 42.14% | `vector::dist::l2_sq_avx2` | the L2 kernel |
| 18.01% | `engine::Engine::exec_distance_topk` | the loop that offers every row to the heap |
| 5.46% | `exec::query::eval_predicate_borrowed` | the filter of the query |
| 4.25% | `value::vector::parse_f32` | the parsing of the numbers of the vector literal |
| 2.35% | `value::vector::Vector::parse_literal` | the parsing of the vector literal |
| 2.31% | `__memcmp_evex_movbe` | the comparison of the strings of a row |
| 1.89% | `_int_malloc` | the allocator |
| 1.18% | `sip::Hasher::write` | the hash of a name |
| 1.18% | `proto::json::write` | the rendering of the answer |
| 1.17% | `sql::lexer::Lexer::lex_string` | the lexer |
| 1.17% | `proto::json::Parser::string` | the parsing of the request |

**The distance computation is 60.15% of the CPU of the server**: 42.14% in
the kernel itself and 18.01% in the loop that calls it. Everything else,
taken together, is under 25%.

---

## 3. What this rules out

### 3.1 The driver is not the problem

An earlier version of this file claimed that a large part of the query time
was the round trip between the driver and the server, and that about 600 ms
of the 949 ms that `tools/bench.py` reports was the Python driver.

That was wrong, and the way it was wrong is worth writing down.

The measurement compared a loop that sent the requests one at a time and
waited for each answer with a loop that sent all of them at once. The two
loops were not comparable: the first one was the **first** thing that ran
against the server and it paid for the page cache, the branch predictors and
the frequency scaling of the machine, while the second one ran on a warm
server. Run the two in turn, and take the median of each:

| Variant | Median |
| --- | ---: |
| One request at a time, wait for each answer | 283 ms |
| All requests sent at once, answers read in the background | 209 ms |
| The same loop, but with the answer parsed as JSON | 281 ms |
| The same loop, but with the answer parsed and the ids converted | 266 ms |

The gap between the first and the last row is about 15 ms, which is the
whole of the parsing cost. The gap between the round trip and the pipelined
run is 74 ms, which is the cost of the synchronisation of the two
processes, not of the driver's work.

And the point that settles it: **`tools/bench.py` is a tool of ours, not
the driver of the platform.** The platform sends the same requests with a
driver of its own, so the overhead of our driver is not part of the score.
There is nothing to win there.

**The rule this gives**: two variants must be run in turn, each with its own
median. Otherwise the one that runs first pays for everything that is
one-time, and the comparison measures the warm-up instead of the thing.

### 3.2 The fixed costs are not worth attacking

The parsing of the vector literal is 6.6% of the CPU (`parse_f32` 4.25% and
`parse_literal` 2.35%). The whole of the request handling and the rendering
of the answer is under 5%. Even halving all of it would buy about 6% of the
query time, while the two items below are worth tens of percent.

---

## 4. What follows from it

In the order in which the work is worth doing:

### 4.1 Fuse the scan and the kernel

The scan calls the kernel once per row
(`minivectordb/src/vector/exact.rs`). So the horizontal sum of the eight
lanes and the branch on the length of the vector are paid **10,000 times per
query**. A kernel that takes the matrix of a whole block at once, and keeps
the accumulator alive across the rows, would remove both.

The risk is that the result changes in the last bit, because the summation
order changes. The gate is the same one every time: the answers must stay
byte for byte identical to the baseline files.

### 4.2 Compute the distance of a row only when it can reach the answer

The heap already drops a row that is worse than the worst candidate it holds
with a single comparison of two floats (`77c2a0ec4`), but by then the
distance of the row **has already been computed**.

If a bound were known before the measurement, the scan could skip the work.
That is the largest prize available: it can remove most of the 60%, not a
few percent of it.

The cost is that the answer is no longer exactly the same. A bound that is
valid cannot change the answer; a bound that is an approximation can. So
this one has to be done with a bound that is **exact**, or not at all. The
safe version of it: the distance of a row is only needed when the row is
*going to enter the heap*, so compute the distance lazily, on the rows that
pass the cheap test.

### 4.3 Apply the filter before the distance

The scan measures the distance of a row **before** the filter is applied to
it, so half of the rows of a filtered query are measured for nothing.

When the filter is an equality on an indexed column, the inverted index
already narrows the candidates down (`a509458a5`), so this only matters for
the filters that the index does not cover. It is 5.46% of the CPU, so the
prize is bounded, but it is the cheapest of the three to get right.

### 4.4 The order to try them in

1. **4.3** first: it is small, it is local, and it cannot change the answer.
2. **4.1** next: it is a contained change in one function, and the gate
   (byte identical answers) tells immediately whether the summation order
   changed.
3. **4.2** last: it is the biggest prize and the one that can change the
   answer, so it needs the exactness argument to be written down first.

---

## 5. The memory of the query phase

The peak memory is read by the platform from `memory.peak` of the cgroup of
the container, so it **includes the page cache** of the files under `/data`.
The local benchmark reports `VmHWM` of the server process, which does not.
The two are different numbers, and the smaller one is the wrong one to
compare against the platform.

What can be measured here are the two parts separately:

| Part | Before the queries | After the queries |
| --- | ---: | ---: |
| Resident set of the server (`VmHWM`) | 8.81 MiB | 14.63 MiB |
| Size of the data directory (the page cache that can hold it) | 10.86 MiB | 10.86 MiB |
| Upper bound of the cgroup peak | 19.68 MiB | 25.50 MiB |

The growth of the resident set over the query phase is the 5.1 MiB of the
decoded vectors, plus the decoded directory and the decoded scalar columns.
The buffer pool of the vectors is what grows, and it is the only part of
the memory that the size of the table decides.

`tools/bench_cgroup_peak.py` measures it the way the platform does, by
running the benchmark in a cgroup of its own. It needs root to create a
cgroup, and on a machine without root it falls back to the resident set and
**says so in its output**, so that a number from the wrong source is never
reported as if it came from the right one.

---

## 6. A note on the numbers

The machine is shared, and the measurement is very sensitive to where the
server runs.

The scheduler moved the server between the cores while it ran. Every move
took the ten megabytes of the decoded blocks out of the cache of the old
core and put them into the cache of the new one, so the scan paid for the
reads again. The same binary measured:

| Where the driver and the server run | Median | Spread |
| --- | ---: | ---: |
| Both free | 703 ms | 49% |
| The server pinned, the driver free | 967 ms | 39% |
| The server pinned to another core than the driver | 922 ms | 105% |
| **Both pinned to the same core** | **327 ms** | **3%** |

So the pinning is not a detail of the measurement: it is worth a factor of
2.2, and without it a comparison of two versions measures the machine.

```sh
python3 tools/bench_median.py --mode exact --rounds 5 --cpu 7
```

A measurement that is going into the ledger has to be taken with the
machine quiet **and pinned**, and the rounds have to be reported as a
range when the spread is wide. A single round is not a measurement, and a
comparison of two binaries that are not run in turn is not a comparison
either.

---

## 7. The commands

```sh
# the profile of section 2
sed -i 's/^strip = true/strip = false/' minivectordb/Cargo.toml
cargo build --offline --locked --release
python3 tools/perf_query_only.py ./minivectordb/target/release/minivectordb \
    exact /tmp/perfwork /tmp/perf.data
perf report -i /tmp/perf.data --stdio --no-children --percent-limit 1.0

# the comparison of section 3.1
python3 tools/probe_roundtrip.py

# the memory of section 5
python3 tools/bench_cgroup_peak.py --mode exact
```
