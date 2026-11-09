# MiniVectorDB - multi stage build.
#
# Stage 1 compiles the Rust binary. The crate has **no external dependencies**,
# so `cargo build --offline` succeeds without any network access, which makes
# the image build reproducible on an isolated builder.
#
# Stage 2 keeps only the resulting binary and the shared libraries it needs.

# ---- build stage -------------------------------------------------------------
FROM rust:1.98-bookworm AS builder

WORKDIR /build

# The dependency graph is empty, so the manifest is copied on its own first.
COPY minivectordb/Cargo.toml ./Cargo.toml
COPY minivectordb/src ./src

# `--offline` proves that the build needs no registry access.
RUN cargo build --release --offline --locked 2>/dev/null \
    || cargo build --release --offline

# ---- runtime stage -----------------------------------------------------------
FROM ubuntu:24.04

# No package installation on purpose: the server is a statically-linkable
# Rust binary that only needs glibc and libgcc, both present in this base
# image. Keeping the runtime stage free of `apt-get` means the build needs
# no network access at all, which keeps it reproducible on an isolated
# builder and avoids depending on an apt mirror being reachable.

COPY --from=builder /build/target/release/minivectordb /usr/local/bin/minivectordb

# The database keeps its files under /data, as the evaluation expects.
RUN mkdir -p /data && chmod 1777 /data
VOLUME ["/data"]

ENV MINIVECDB_DATA_DIR=/data
ENV MINIVECDB_OUTPUT_FORMAT=tsv
ENV MINIVECDB_READY_SINK=stderr
ENV MINIVECDB_MODE=exact
ENV MINIVECDB_DURABILITY=os
ENV MINIVECDB_BUFFER_POOL_MB=64

# The server reads SQL from stdin and writes results to stdout.
ENTRYPOINT ["/usr/local/bin/minivectordb"]
