#!/usr/bin/env bash
# Measure the performance of the database inside a container.
#
# The measurement has to happen inside the container: the benchmark reads the
# I/O counters and the peak memory of the server process out of /proc, and
# outside the container those counters belong to the docker client, not to
# the database.
#
# The benchmark runs in the build stage image, which holds a python, and
# the database binary is mounted into it. The tools directory is mounted as
# well, so the benchmark that runs there is the very same one that runs on
# the host.
#
# Usage:
#   tools/bench_container.sh [--rows N] [--dim N] [--queries N] [--mode M]

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${MINIVECDB_CONTAINER_IMAGE:-mvdb-final}"
RUNNER="${MINIVECDB_BENCH_RUNNER:-rust:1.98-bookworm}"

# The benchmark needs a data directory of its own.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/mvdb-bench-container-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

docker run --rm -i \
    -v "$WORK":/work \
    -v "$ROOT/tools":/tools:ro \
    -v "$ROOT/minivectordb/target/release/minivectordb":/usr/local/bin/minivectordb:ro \
    -u "$(id -u):$(id -g)" \
    --entrypoint python3 \
    "$RUNNER" \
    /tools/bench.py \
    --binary /usr/local/bin/minivectordb \
    --data-dir /work/data \
    "$@"
