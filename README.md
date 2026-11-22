# Mech Royale Bot — Apex-Go v3

Submission for the **Mech Royale** Builders Buildathon track. The repository root
has the `Dockerfile` required by the scorer.

`apex-go` is a statically linked linux/amd64 build of **Apex-Go v3** from the
MIT-licensed `ahmad-ovh/Buildathon` repository, built from the matching source in
`cmd/` and `internal/` (see `LICENSE-APEX`). Apex-Go v2 scored **6.28** official
placement points and was #1 on the practice board; v3 adds the five
zero-overfit upgrades described in that repository's `docs/STATE.md` (predicted
**6.45–6.75** official).

The build is a `FROM scratch` image with the single static binary, so the
scorer's build step only copies 3.2 MB and cannot fail on network/toolchain
availability.

## Verify

```sh
# Protocol conformance with the official kit
python3 mechfight-kit/check/check.py -- ./apex-go

# Reproduce the build yourself (Go 1.25+)
GOFLAGS=-mod=mod CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o apex-go ./cmd/mechbot
```

Measured locally: ready in ~1.7 s, **300/300** answers in budget, p99 ~12.5 ms,
max ~27 ms.

## Submission log

| Commit | Idea | Official score |
|---|---|---|
| r1ver baseline | MIT TTUN/r1ver economy bot | 4.99 |
| Apex-Go v2 | vendored #1 bot (`3af1ada` on `apex-go`) | 6.28 |
| Apex-Go v3 | this build | TBD |
