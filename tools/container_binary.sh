#!/bin/sh
# Run the database inside a container, with the same interface as the
# binary itself.
#
# The test drivers start the database as a child process and talk to it
# over standard input and standard output. This wrapper starts a container
# instead, so that the drivers can check the image itself and not only the
# binary that was built on the host.
#
# The options of the database are passed through the way the drivers set
# them: the environment variables are handed to the container, and the
# command line is forwarded. The data directory of the driver is mounted at
# /data, which is where the image keeps its files, so a `--data-dir` on the
# command line is rewritten to that path.

set -e

IMAGE="${MINIVECDB_CONTAINER_IMAGE:-mvdb-final}"
DATA_DIR="${MINIVECDB_DATA_DIR:-/tmp/mvdb-container-data}"

# Collect the arguments, and rewrite the data directory.
ARGS=""
NEXT_IS_DATA_DIR=0
for arg in "$@"; do
    if [ "$NEXT_IS_DATA_DIR" = "1" ]; then
        DATA_DIR="$arg"
        NEXT_IS_DATA_DIR=0
        continue
    fi
    case "$arg" in
        --data-dir)
            NEXT_IS_DATA_DIR=1
            ;;
        --data-dir=*)
            DATA_DIR="${arg#--data-dir=}"
            ;;
        *)
            ARGS="$ARGS $(printf '%s' "$arg" | sed "s/'/'\\\\''/g; s/^/'/; s/\$/'/")"
            ;;
    esac
done

mkdir -p "$DATA_DIR"

# The container runs as the user of the host, so that the files it writes
# belong to that user and the host can remove them again. The image gives
# the data directory the permissions of everybody, so the user can write
# into it.
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

# The arguments are handed to the container through the environment, so
# that the quoting of the wrapper does not have to survive the trip. The
# container reads them back with `eval`, which restores the quoting.
MINIVECDB_CONTAINER_ARGS="$ARGS"
export MINIVECDB_CONTAINER_ARGS

exec docker run --rm -i \
    -v "$DATA_DIR":/data \
    -u "$HOST_UID:$HOST_GID" \
    -e MINIVECDB_MODE \
    -e MINIVECDB_OUTPUT_FORMAT \
    -e MINIVECDB_READY_SINK \
    -e MINIVECDB_ANN_NLIST \
    -e MINIVECDB_ANN_NPROBE \
    -e MINIVECDB_CONTAINER_ARGS \
    --entrypoint sh \
    "$IMAGE" \
    -c 'eval "exec /usr/local/bin/minivectordb --data-dir /data $MINIVECDB_CONTAINER_ARGS"'
