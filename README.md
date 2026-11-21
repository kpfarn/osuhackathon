# Mech Royale Bot — "Cover Circuit"

Submission for the **Mech Royale** Builders Buildathon track (16-mech battle
royale on the OSU campus). The image is built from this folder; `Dockerfile` is
at the repository root.

This bot is adapted from the MIT-licensed **Cover Circuit** project
(`emptybook11/mech-royale-bot`). The original `LICENSE` is included. The main
change from the previous submission is architectural: instead of a pure hider,
this bot plays for **placement through combat and looting** — the practice squad
does not eliminate itself, so outliving it requires thinning it.

## Files

- `bot.py` — zone routing, resource looting, upgrades, retreat, engagement.
- `arena.py` — radius-aware navigation grid, exact prism occlusion, bounded A*.
- `combat.py` — predictive gun aim, mortar ballistics, laser/lightning gating.
- `mechfight.py` — official Python protocol wrapper (unchanged from the kit).
- `Dockerfile` — Python 3.13, standard library only.
- `LICENSE` — MIT license for the adapted source.
- `dev/legacy/` — the previous "Ghost" hider bot and its local simulator, kept
  for reference. The Docker image copies only the four bot files.

## How to test locally

```sh
# Protocol conformance (keep mechfight-kit locally; it is gitignored)
python3 mechfight-kit/check/check.py -- python3 -u bot.py

# With the match's map path
python3 mechfight-kit/check/check.py --map-dir /arena -- python3 -u bot.py

# Docker build (no network at runtime)
docker build -t cover-circuit .
```

Measured on the dev machine: ready in ~1.1 s, p99 ~16–18 ms, max ~40 ms,
300/300 answers within budget.

## Strategy

1. **Pre-position into the next circle.** It plans a route to a point inside the
   next ring rather than camping on the current rim, so it is never crossing
   open ground late and is near supply drops when they land.
2. **Loot while safe.** It remembers weapons, crates, cubes, and drops after
   looking away, then fetches the best value per metre travelled. Power cubes
   are max HP, which matters in the final collapse.
3. **Fight when the odds are good.** It strafes at ~40–60 m and uses predictive
   aim for the gun, the laser, lightning, and mortars. It targets wounded mechs
   first, because a kill is a placement point.
4. **Retreat at low HP.** Below ~59% HP, or when surrounded, it breaks contact,
   seeks terrain cover, and stops firing so regen can start.
5. **Dodge projectiles.** Incoming gun rounds and mortar shells are predicted
   and side-stepped; mortar arcs are handled ballistically.
6. **No file writes and no network**, so it runs under the scorer's read-only,
   unprivileged sandbox.

## Submission log

| # | Commit | Idea tested | Score | Notes |
|---|--------|-------------|-------|-------|
| 1 | (initial) | Stealth / hide & farm | 0.13 | Too passive; died first |
| 2 | | Survival vs coordinated squad | 0.06 | Worse |
| 3 | | Potential fields, cover-seeking | 0.42 | Survived longer, still outlived nobody |
| 4 | | A* nav, real cover, laser breaks | 0.42 | |
| 5 | | NPC/mortar avoidance, cube/drop looting | 0.70 | |
| 6 | | Weapon pickup, laser/lightning, finish wounded | 0.83 | Pure-hider ceiling |
| 7 | | Cover Circuit: combat + loot + retreat | TBD | Architecture change |

## Attribution

`bot.py`, `arena.py`, and `combat.py` are adapted from
[`emptybook11/mech-royale-bot`](https://github.com/emptybook11/mech-royale-bot),
released under the MIT license. `mechfight.py` is the official starter-kit
wrapper. The previous Ghost bot remains in `dev/legacy/` and in git history.
