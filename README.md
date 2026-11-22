# Mech Royale Bot — TTUN / r1ver baseline

Submission for the **Mech Royale** Builders Buildathon track. The image is
built from this folder; `Dockerfile` is at the repository root.

THIS REPO PLACED 9th OUT OF OVER 400 ENTRIES.

This is the MIT-licensed **TTUN / r1ver** economy bot, the exact baseline
published by team r1ver. It is vendored and submitted unchanged except for the
Docker entrypoint. That revision scored **5.10** official practice points for
r1ver; team r1ver is **rank 16 at 5.52** on the current builders-4 board
(top-32 qualifying cutoff: 5.19).

## Files

- `bot.ts` — the bot: economy, looting, zone routing, combat, upgrades.
- `map.ts` — terrain/building map and navigation.
- `mechfight.ts` — Bun/TypeScript protocol wrapper.
- `Dockerfile` — Bun runtime, cache redirected to `/tmp` for the read-only sandbox.
- `LICENSE-TTUN` — MIT license for the r1ver source.
- `dev/legacy/` — previous Python bots (Cover Circuit and Ghost) kept for reference;
  not copied into the image.

## How to test locally

```sh
# Protocol conformance (requires Bun)
python3 mechfight-kit/check/check.py -- bun bot.ts

# With the match's map path
python3 mechfight-kit/check/check.py --map-dir /arena -- bun bot.ts

# Docker build (no network at runtime)
docker build -t ttun-bot .
```

Measured: ready in ~0.5 s, p99 ~24 ms, max ~28 ms, 300/300 answers in budget.

## Strategy

- Economy first: keep the gun plus the best special, detour for cubes, weapons,
  crates, drones, and supply drops when the route is safe.
- Upgrade priority: plating, armor, nanites, then weapon upgrades.
- Fight with target focus and predictive aim; use cover when losing.
- Approach the zone ahead of the shrink; jump/jet recovery when stuck.

## Submission log

| # | Idea | Score | Notes |
|---|------|-------|-------|
| 1–6 | Stealth / Ghost / Cover Circuit variants | 0.15 → 4.9 | Best builders-3 result; 4.61 on builders-4 |
| 7 | TTUN / r1ver baseline (this build) | TBD | Known 5.10 builders-3; team r1ver 5.52 builders-4 |

## Attribution

`bot.ts`, `map.ts`, and `mechfight.ts` are the MIT-licensed TTUN / r1ver
implementation, vendored via
[`Evans-Kwashie/mech-royale-bot`](https://github.com/Evans-Kwashie/mech-royale-bot)
(`baselines/r1ver`, original revision `af337d5c1275d03478685bd4936c8a551fa6b200`).
Copyright (c) 2026 TTUN; see `LICENSE-TTUN`.
