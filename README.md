# Mech Royale Bot — "Ghost" (survival-focused)

This repository contains a Python bot for the **Mech Royale** buildathon track
(16-mech battle royale on the OSU campus). The bot is packaged as a Docker image
with the `Dockerfile` at the repository root, as required by the scorer.

## Files

- `Dockerfile` — builds the image (Python 3.13, standard library only).
- `mechfight.py` — protocol wrapper (stdin/stdout, newest-observation rule, debug drawing).
- `arena.py` — fast static-map queries (walkability, line of sight, terrain, trees).
- `ghost.py` — the bot logic (current submission).
- `stealth_bot.py` — the previous bot (kept for reference; not used by the image).
- `sim.py` — offline testbed used during development (not imported by the bot).
- `mechfight-kit/` — original starter kit (protocol, schema, check, map, examples).

## How to test locally

```sh
# Protocol conformance (keep mechfight-kit locally; it is gitignored)
python3 mechfight-kit/check/check.py -- python3 -u ghost.py

# With the match's map path
python3 mechfight-kit/check/check.py --map-dir /arena -- python3 -u ghost.py

# Behavioural testbed (dev only): 20 seeded matches against one sniper
python3 sim.py -n 20 -t sniper

# Docker build (no network at runtime)
docker build -t ghost-bot .
```

## Submission log

Track each submission here so score changes can be attributed to ideas.

| # | Commit | Idea tested | Score | Notes |
|---|--------|-------------|-------|-------|
| 1 | (initial) | Stealth / hide & farm | 0.13 | Too passive; died first often |
| 2 | | Survival vs coordinated squad | 0.06 | Worse; still dying early |
| 3 | | + potential fields, cover-seeking | TBD | |
| 4 | | + air play (jet when spotted) | TBD | |
| 5 | | Ghost rewrite: A* nav, real cover, laser breaks | TBD | median ~400 s vs one sniper; ~330-380 s vs 2-3 |

> Update this table after each submission.

## Core problem (from track page)

- Practice = **1 bot (you) vs 15 house bots** acting as **one coordinated squad**.
- They **share vision**, **never fight each other**, and **hunt the weakest mech first**
  (one early, more as you thin them out, all of them late).
- **Placement counts, not kills**: dying first with 5 kills scores worse than
  surviving to the end with none.
- 50 ms decision budget; 1 CPU / 512 MB; no network; read-only disk except `/tmp`.
- 4 submissions per hour.

Implication: **survival > aggression**. Engaging the squad is almost always fatal.
The goal is to stay alive for ~7 minutes while the zone closes.

## Current strategy ("Ghost")

The rewrite in `ghost.py` is built around one rule: **never be exposed for longer
than a laser charge (0.75 s)**.

1. **Cover is geometric, not reactive.** Building faces are pre-indexed as
   standable "sites". A site is used only when line of sight from every known
   threat is blocked (`segment_blocked` against prisms and terrain).
2. **When a laser charges** (seen `charging`, heard `laser_charge`, or a hit just
   landed) the bot computes the *nearest* patch of truly hidden ground and
   sprints to it. It ignores the zone and the gun until it is hidden again.
3. **Movement is committed.** A 5 m coarse A* raster (baked once at start) gives
   a route; a local wall-following steer executes it with hysteresis, so the bot
   no longer dithers between two directions.
4. **Hidden does not mean safe.** While hidden from a known hunter the bot keeps
   relocating 10-40 m along *covered corridors*, so a hunter arriving at the last
   seen position does not find it standing there.
5. **The zone is followed at depth, not at the edge**: it targets a point well
   inside the next circle, and only abandons cover for the zone when staying out
   would be certain death.
6. **The gun is opportunistic**: fire only when already aimed, in range, and the
   shot is not blocked. No fights are picked.
7. **No jets.** `boost` is suppressed in the air (a held jet climbs until the
   match ends); jumps are used only to clear low steps.
8. **Performance**: per-tick work is capped by a wall-clock budget; measured
   p99 is ~33 ms and max ~39 ms on the dev machine (`check.py` passes clean).

Local results (`sim.py`, one sniper that tracks perfectly): median survival
~400 s, with most matches ending only when the zone itself collapses. Against
two or three snipers the median is ~330-380 s. The previous bot died at ~40 s.

## Idea backlog (consider every angle)

### Mobility / air play
- [ ] Jet onto rooftops/bridges and hold high ground (house bots may not follow).
- [ ] Climb stadium/roof edges using step-up + jet.
- [ ] Use bridges as cover (walk/shoot under them).
- [ ] Randomized long paths around buildings to avoid interception.
- [ ] Zone-edge surfing: circle the perimeter so hunters take zone damage.

### Stealth / detection
- [ ] Dense tree clusters as cover (crowns block vision; trunks stop it).
- [ ] Stand completely still in cover to regenerate HP and avoid step sounds.
- [ ] Move only when not spotted; freeze when line-of-sight is broken.
- [ ] Use terrain depressions / low ground.
- [ ] Break line-of-sight then change direction (avoid predictable pursuit).

### Survival / HP
- [ ] Prioritize armor upgrades to reduce damage.
- [ ] Avoid all fights; HP regen after 5 s out of combat.
- [ ] Stay high-HP to delay "weakest first" targeting.
- [ ] Use energy for jet escapes; conserve otherwise.

### Zone
- [ ] Always move toward next center; never linger in outer ring.
- [ ] Use zone edge as a wall (hunters hesitate to enter late zone).
- [ ] Pre-position before shrink starts.

### Sensing / awareness
- [ ] Sound-based threat detection (gun/mortar/explosion bearings).
- [ ] Track remembered enemy positions (5 s memory).
- [ ] Scan aim to detect hunters early (costs aim time).
- [ ] Use proximity (45 m) sensors; buildings block, canopy doesn't.

### Upgrades
- [ ] Sensors (vision + sound) for early detection.
- [ ] Speed/turn for evasion.
- [ ] Armor for survivability.
- [ ] Avoid weapon upgrades (we don't fight).

### Movement tech
- [ ] Strafe perpendicular to incoming fire.
- [ ] Jitter/randomization to avoid being led.
- [ ] Wall-sliding to navigate without getting stuck.
- [ ] Jump over small obstacles instead of pathing around.

### Optimization
- [ ] Keep answer time well under 50 ms (currently ~15 ms p99).
- [ ] Cache map data; avoid per-tick heavy loops.
- [ ] Use numpy if needed (build has network).

### Experimental variants to submit
- A. **Pure hider**: no farming, no air play, maximum stillness in cover.
- B. **Runner**: max speed, circle zone, no hiding.
- C. **Roof camper**: climb and hold high ground.
- D. **Hybrid**: hide when spotted, run when not, farm only if isolated.

## Notes

- `check/check.py` is not a simulation; passing it only confirms protocol compliance.
- The map is at `/arena` in matches; terrain is `v2/terrain.i16` (int16 cm).
- Prisms (buildings/bridges) block movement, sight, and shots; trees block vision only.
- Water does not slow you; barriers/fences are ignored by the server.

## License

Starter kit provided by the Mech Royale track. Bot code original.