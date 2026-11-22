# Build from this folder: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM scratch
COPY apex-go /mechbot
USER 65534:65534
ENTRYPOINT ["/mechbot"]
