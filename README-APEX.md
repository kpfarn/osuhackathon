# Apex-Go v2 (MIT)

This branch is the current #1 practice bot ("Ahmad Suliman", 6.28 placement
points) from the MIT-licensed `ahmad-ovh/Buildathon` repository, commit
`a484109`.

- `apex-go`: prebuilt static linux/amd64 binary (built from `cmd/mechbot`).
- `cmd/`, `internal/`, `go.mod`: the corresponding source, for review.
- `LICENSE-APEX`: MIT license, copyright (c) 2026 ahmad-ovh.

The binary passes the official conformance check (300/300, p99 ~10 ms).

**Note:** the track has a rule that "excessive code reuse may result in
disqualification." Submitting this exact bot is maximum reuse; use it only if
that risk is acceptable. The `main` branch holds the safer r1ver baseline.
