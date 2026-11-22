# Prebuilt static linux/amd64 binary from the MIT Apex-Go v2 source in this repo.
FROM scratch
COPY apex-go /mechbot
USER 65534:65534
ENTRYPOINT ["/mechbot"]
