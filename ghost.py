"""Ghost -- a survival bot for Mech Royale.

The practice lobby is one bot against a squad of house bots that share vision
and hunt placements, not kills.  This bot therefore optimises for *never being
seen*, and for breaking line of sight within a fraction of a second when it is.

  * movement is planned on a coarse navigation raster (A*) and executed with a
    committed heading, so the bot paths around buildings instead of dithering;
  * cover is chosen geometrically: a point on a building face that cannot be
    seen from the known threats, close to the bot, and toward the zone;
  * when a laser starts charging (seen or heard) and the bot is exposed, it
    drops everything and sprints to the nearest point that is actually hidden;
  * while hidden it relocates along *covered corridors*, so a hunter arriving
    at the last seen position does not walk straight into it;
  * the zone is tracked by projecting the bot into the next circle with a
    margin, so it never has to cross open ground late;
  * the gun is opportunistic only: fire when already aimed, in range, and the
    shot is not blocked.

Geometry comes from arena.py; this file is pure decision making.
"""
import gc
import heapq
import math
import random
import time

import mechfight as mf
from arena import Arena

TAU = 2.0 * math.pi
INF = 1e18

# ordered offsets tried when the straight line is blocked
STEER_OFFSETS = (0.30, -0.30, 0.62, -0.62, 0.95, -0.95,
                 1.30, -1.30, 1.70, -1.70, 2.10, -2.10, 2.55, -2.55)


def ang_diff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def unit(x, y):
    d = math.hypot(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


class Site:
    """A standing spot just outside a building face."""
    __slots__ = ("x", "y", "z", "nx", "ny", "bidx", "cov")

    def __init__(self, x, y, z, nx, ny, bidx):
        self.x = x
        self.y = y
        self.z = z
        self.nx = nx
        self.ny = ny
        self.bidx = bidx
        self.cov = -1


class Bot:
    name = "Ghost"

    # ---------------------------------------------------------------- setup
    def __init__(self, hello):
        gc.disable()  # no cyclic garbage; avoids multi-tick GC pauses
        cfg = hello["config"]
        self.cfg = cfg
        self.rng = random.Random(int(hello["seed"]) ^ (int(hello["slot"]) * 7919))
        self.arena = hello.get("_arena") or Arena(hello["map"]["dir"])
        a = self.arena

        m = cfg["mech"]
        self.radius = m["radius"]
        self.height = m["height"]
        self.eye_h = m["eye_height"]
        self.muzzle_h = m["muzzle_height"]
        self.step_up = m["step_up"]
        self.walk = m["walk_speed"]
        self.boost_speed = m["boost_speed"]
        self.turn = m["turn_rate"]
        v = cfg["vision"]
        self.vrange = v["range"]
        self.near_range = v["near_range"]
        self.fov = math.radians(v["fov_deg"]) * 0.5
        w = cfg["weapons"]
        self.gun = w["gun"]
        self.laser = w.get("laser")
        self.mortar = w.get("mortar")
        self.lightning = w.get("lightning")
        self.zone_cfg = cfg["zone"]
        self.loot_r = cfg["loot"]["pickup_radius"]
        self.cube_r = cfg["cubes"]["pickup_radius"]

        self.t = -1.0
        self.pos = [0.0, 0.0, 0.0]
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.hp = 1400.0
        self.max_hp = 1400.0
        self.energy = 100.0
        self.grounded = True
        self.in_zone = True
        self.cubes = 0
        self.weapons = [None, None]

        self.known = {}
        self.enemies = []
        self.threats = []
        self.charge_threat = False
        self.last_damage_t = -99.0
        self.last_damage_bearing = None
        self.seen_real = False

        # cover spots
        self.sites = []
        self.site_grid = {}
        self.site_cell = 32.0
        self._build_sites()

        # navigation
        self.ng = 5.0
        self.nnx = int((a.bounds[1] - a.gx0) / self.ng) + 2
        self.nny = int((a.bounds[3] - a.gy0) / self.ng) + 2
        self.nwalk = bytearray(self.nnx * self.nny)
        self._bake_nav()
        self.path = []
        self.path_goal = None
        self.path_t = -99.0
        self.path_fail = 0

        # goal commitment
        self.goal = None
        self.goal_kind = None
        self.goal_t = -99.0
        self.anchor = None
        self.anchor_t = -99.0
        self.anchor_hold = 0.0
        self.esc_goal = None
        self.esc_t = -99.0
        self.zone_forced = False
        self.loot = None
        self.loot_t = -99.0
        self.loot_deadline = 0.0
        self.loot_dead = {}
        self.loot_black = {}
        self.loot_step = None
        self.loot_step_t = -99.0
        self.mortar_danger = False
        self.crate_aim = None
        self.res_mem = {}

        # execution
        self.mode = "init"
        self.turn_bias = 0.0
        self.want_jump = False
        self.stuck = 0
        self.stuck_t = -99.0
        self.prev_pos = None
        self.escape_until = -1.0
        self.escape_dir = 0.0
        self.prev_move = (0.0, 0.0)
        self._exp_cache = {}
        self._t0 = 0.0

    def _late(self, budget=0.030):
        if not self._t0:
            return False
        return time.perf_counter() - self._t0 > budget

    # ------------------------------------------------------------- geometry
    def _build_sites(self):
        a = self.arena
        for bi, p in enumerate(a.prisms):
            if p.top - p.base < 3.0:
                continue
            poly = p.poly
            n = len(poly)
            area = 0.0
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                area += x1 * y2 - x2 * y1
            ccw = area > 0
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                ex = x2 - x1
                ey = y2 - y1
                el = math.hypot(ex, ey)
                if el < 1.5:
                    continue
                if ccw:
                    nx, ny = ey / el, -ex / el
                else:
                    nx, ny = -ey / el, ex / el
                steps = max(1, int(el / 7.0))
                for k in range(steps + 1):
                    f = k / steps
                    bx = x1 + ex * f
                    by = y1 + ey * f
                    for off in (5.5, 4.2, 7.5):
                        sx = bx + nx * off
                        sy = by + ny * off
                        ok, h = a.walkable(sx, sy, a.height_fast(sx, sy))
                        if ok:
                            self.sites.append(Site(sx, sy, h, nx, ny, bi))
                            break
        for s in self.sites:
            key = (int(s.x // self.site_cell), int(s.y // self.site_cell))
            self.site_grid.setdefault(key, []).append(s)

    def near_sites(self, x, y, r):
        c = self.site_cell
        out = []
        for i in range(int((x - r) // c), int((x + r) // c) + 1):
            for j in range(int((y - r) // c), int((y + r) // c) + 1):
                for s in self.site_grid.get((i, j), ()):
                    if (s.x - x) ** 2 + (s.y - y) ** 2 <= r * r:
                        out.append(s)
        return out

    # -------------------------------------------------------------- threats
    def _update_threats(self, obs):
        t = obs["t"]
        npcs = []
        for m in obs["mechs"]:
            if m["kind"] == "npc":
                px0, py0 = self.pos[0], self.pos[1]
                d = math.hypot(m["pos"][0] - px0, m["pos"][1] - py0)
                if d < 55.0:
                    npcs.append({"id": m["id"], "pos": list(m["pos"]),
                                 "vel": m.get("vel", [0, 0, 0]),
                                 "w": 0.5, "weapons": [], "charging": False,
                                 "hp": m.get("hp"), "real": False,
                                 "eye": 2.0})
                continue
            if m["kind"] != "mech":
                continue
            self.known[m["id"]] = {
                "pos": list(m["pos"]), "vel": m.get("vel", [0, 0, 0]),
                "t": t, "hp": m.get("hp"), "weapons": m.get("weapons", []),
                "charging": bool(m.get("charging", False)),
            }
        for k in list(self.known):
            if t - self.known[k]["t"] > 9.0:
                del self.known[k]

        px, py = self.pos[0], self.pos[1]
        self.enemies = list(npcs)
        self.seen_real = False
        for k, e in self.known.items():
            self.seen_real = True
            self.enemies.append({"id": k, "pos": e["pos"], "vel": e["vel"],
                                 "w": 1.0, "weapons": e["weapons"],
                                 "charging": e["charging"], "hp": e.get("hp"),
                                 "real": True})
        # sounds: rough bearings, decaying confidence
        carry = {"gun": 1, "laser": 1, "laser_charge": 1, "mortar": 1,
                 "explosion": 1, "lightning": 1, "jet": 0.5, "boost": 0.5,
                 "step": 0.4, "reload": 0.3}
        for s in obs.get("sounds", []):
            kind = s.get("kind", "")
            cf = carry.get(kind)
            if cf is None:
                continue
            age = s.get("age", 0.0)
            if age > 2.5:
                continue
            b = s.get("bearing", 0.0)
            dc = s.get("dist", "far")
            dist = 34.0 if dc == "near" else (105.0 if dc == "mid" else 210.0)
            gx = px + math.cos(b) * dist
            gy = py + math.sin(b) * dist
            self.enemies.append({"id": -1, "pos": [gx, gy, self.arena.height_fast(gx, gy)],
                                 "vel": [0, 0, 0],
                                 "w": 0.6 * cf * max(0.0, 1.0 - age / 2.5),
                                 "weapons": [], "charging": kind == "laser_charge",
                                 "hp": None, "real": False})
        # a hit is proof that *something* has a line to us right now
        age = t - self.last_damage_t
        if age < 2.0 and self.last_damage_bearing is not None:
            b = self.last_damage_bearing
            gx = px + math.cos(b) * 110.0
            gy = py + math.sin(b) * 110.0
            self.enemies.append({"id": -2, "pos": [gx, gy, self.arena.height_fast(gx, gy)],
                                 "vel": [0, 0, 0], "w": 1.0,
                                 "weapons": [], "charging": age < 1.0,
                                 "hp": None, "real": False})
        self.enemies.sort(key=lambda e: (e["pos"][0] - px) ** 2 + (e["pos"][1] - py) ** 2)
        self.threats = self.enemies[:4]
        self.charge_threat = any(
            e.get("charging") and (e["pos"][0] - px) ** 2 + (e["pos"][1] - py) ** 2 < 300 ** 2
            for e in self.threats)

    # ------------------------------------------------------------ exposure
    def _exposure(self, x, y, threats, z=None, weight=True):
        a = self.arena
        if z is None:
            z = a.height_fast(x, y)
        centre = (x, y, z + self.height * 0.5)
        top = (x, y, z + self.height)
        total = 0.0
        for n, e in enumerate(threats):
            ex, ey, ez = e["pos"]
            d = math.hypot(ex - x, ey - y)
            if d > 330.0:
                continue
            key = (n, int(x), int(y))
            hit = self._exp_cache.get(key)
            if hit is None:
                eye = (ex, ey, ez + e.get("eye", self.eye_h))
                hit = a.visible(eye, centre, top, terrain=True)
                self._exp_cache[key] = hit
            if hit:
                w = e["w"] if weight else 1.0
                k = 1.0 if d < 110.0 else max(0.25, 1.0 - (d - 110.0) / 260.0)
                total += w * k
        return total

    def _hidden(self, x, y, threats, z=None):
        """True only if *every* threat is blocked; stops at the first sighting."""
        a = self.arena
        if z is None:
            z = a.height_fast(x, y)
        centre = (x, y, z + self.height * 0.5)
        top = (x, y, z + self.height)
        for n, e in enumerate(threats):
            ex, ey, ez = e["pos"]
            if (ex - x) ** 2 + (ey - y) ** 2 > 330.0 * 330.0:
                continue
            key = (n, int(x), int(y))
            hit = self._exp_cache.get(key)
            if hit is None:
                eye = (ex, ey, ez + e.get("eye", self.eye_h))
                hit = a.visible(eye, centre, top, terrain=True)
                self._exp_cache[key] = hit
            if hit:
                return False
        return True

    def _corridor_hidden(self, x1, y1, x2, y2, threats):
        """True when the straight walk stays out of sight (sampled)."""
        d = math.hypot(x2 - x1, y2 - y1)
        n = max(2, int(d / 5.0))
        for k in range(1, n + 1):
            f = k / (n + 1)
            if self._exposure(x1 + (x2 - x1) * f, y1 + (y2 - y1) * f, threats) > 0.0:
                return False
        return True

    def _concealment(self, s):
        """How enclosed a spot is: share of 8 long rays that hit a building."""
        if s.cov >= 0:
            return s.cov
        a = self.arena
        n = 0
        eye = (s.x, s.y, s.z + self.height * 0.4)
        for k in range(8):
            ang = k * TAU / 8.0 + 0.3
            far = (s.x + math.cos(ang) * 95.0, s.y + math.sin(ang) * 95.0,
                   s.z + self.height * 0.4)
            if a.segment_blocked(eye, far, terrain=False):
                n += 1
        s.cov = n
        return n

    # ------------------------------------------------------------- planner
    def _bake_nav(self):
        """Fill the coarse walk raster once; keeps per-tick A* cheap."""
        a = self.arena
        g = self.ng
        for j in range(self.nny):
            y = a.gy0 + (j + 0.5) * g
            row = j * self.nnx
            for i in range(self.nnx):
                x = a.gx0 + (i + 0.5) * g
                ok = True
                for ox, oy in ((0.0, 0.0), (2.6, 0.0), (-2.6, 0.0),
                               (0.0, 2.6), (0.0, -2.6)):
                    w, _ = a.walkable(x + ox, y + oy, a.height_fast(x + ox, y + oy))
                    if not w:
                        ok = False
                        break
                self.nwalk[row + i] = 1 if ok else 2

    def _nav_walkable(self, i, j):
        if i < 0 or j < 0 or i >= self.nnx or j >= self.nny:
            return False
        return self.nwalk[j * self.nnx + i] == 1

    def _nearest_nav(self, gi, gj, r=4):
        if self._nav_walkable(gi, gj):
            return gi, gj
        best = None
        bd = 1e9
        for rad in range(1, r + 1):
            for di in range(-rad, rad + 1):
                for dj in range(-rad, rad + 1):
                    if max(abs(di), abs(dj)) != rad:
                        continue
                    ni, nj = gi + di, gj + dj
                    if self._nav_walkable(ni, nj):
                        d = di * di + dj * dj
                        if d < bd:
                            bd = d
                            best = (ni, nj)
            if best is not None:
                return best
        return None

    def _clear_line(self, x1, y1, x2, y2):
        a = self.arena
        d = math.hypot(x2 - x1, y2 - y1)
        n = max(1, int(d / 2.5))
        z = a.height_fast(x1, y1)
        for k in range(1, n + 1):
            f = k / n
            x = x1 + (x2 - x1) * f
            y = y1 + (y2 - y1) * f
            ok, _ = a.walkable(x, y, z)
            if not ok:
                return False
        return True

    def plan(self, tx, ty, max_expand=4000):
        a = self.arena
        gnx, gny = self.nnx, self.nny
        si = int((self.pos[0] - a.gx0) / self.ng)
        sj = int((self.pos[1] - a.gy0) / self.ng)
        gi = int((tx - a.gx0) / self.ng)
        gj = int((ty - a.gy0) / self.ng)
        if not (0 <= si < gnx and 0 <= sj < gny and 0 <= gi < gnx and 0 <= gj < gny):
            return []
        st = self._nearest_nav(si, sj, 3)
        go = self._nearest_nav(gi, gj, 3)
        if st is None or go is None:
            return []
        si, sj = st
        gi, gj = go
        start = sj * gnx + si
        goal = gj * gnx + gi
        if start == goal:
            return [(tx, ty)]
        # only search the corridor between start and goal, never the whole map
        pad = 26
        i0 = max(0, min(si, gi) - pad)
        i1 = min(gnx - 1, max(si, gi) + pad)
        j0 = max(0, min(sj, gj) - pad)
        j1 = min(gny - 1, max(sj, gj) + pad)
        openh = [(0.0, start)]
        gscore = {start: 0.0}
        came = {start: None}
        closed = set()
        n = 0
        while openh:
            f, cur = heapq.heappop(openh)
            if cur in closed:
                continue
            if cur == goal:
                break
            closed.add(cur)
            n += 1
            if n > max_expand:
                return []
            if n % 256 == 0 and self._late(0.028):
                return []
            ci = cur % gnx
            cj = cur // gnx
            cg = gscore[cur]
            for di, dj, cost in ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)):
                ni = ci + di
                nj = cj + dj
                if ni < i0 or ni > i1 or nj < j0 or nj > j1:
                    continue
                if not self._nav_walkable(ni, nj):
                    continue
                if di and dj and (not self._nav_walkable(ci + di, cj)
                                  or not self._nav_walkable(ci, cj + dj)):
                    continue
                nidx = nj * gnx + ni
                ng = cg + cost
                if ng < gscore.get(nidx, INF):
                    gscore[nidx] = ng
                    came[nidx] = cur
                    hx = abs(gi - ni)
                    hy = abs(gj - nj)
                    h = max(hx, hy) + 0.414 * min(hx, hy)
                    heapq.heappush(openh, (ng + h, nidx))
        if goal not in came:
            return []
        raw = []
        cur = goal
        while cur != start:
            raw.append(cur)
            cur = came.get(cur)
            if cur is None:
                return []
        raw.append(start)
        raw.reverse()
        pts = [(a.gx0 + (c % gnx + 0.5) * self.ng,
                a.gy0 + (c // gnx + 0.5) * self.ng) for c in raw]
        # greedy shortcutting keeps the route but drops zig-zags
        out = [pts[0]]
        i = 0
        while i < len(pts) - 1:
            j = min(len(pts) - 1, i + 14)
            while j > i + 1 and not self._clear_line(pts[i][0], pts[i][1],
                                                     pts[j][0], pts[j][1]):
                j -= 1
            out.append(pts[j])
            i = j
        return out

    def _waypoint(self, gx, gy, now):
        """Next point to steer at; recomputes the A* route when needed."""
        key = (gx, gy)
        stale = (self.path_goal is None
                 or math.hypot(self.path_goal[0] - gx, self.path_goal[1] - gy) > 10.0
                 or now - self.path_t > 2.5
                 or not self.path)
        if stale:
            self.path = self.plan(gx, gy)
            self.path_goal = key
            self.path_t = now
            if not self.path:
                self.path_fail += 1
        x, y = self.pos[0], self.pos[1]
        while len(self.path) > 1 and math.hypot(self.path[0][0] - x,
                                                self.path[0][1] - y) < 3.6:
            self.path.pop(0)
        if not self.path:
            return gx, gy
        return self.path[0]

    # -------------------------------------------------------------- zone
    def _zone_goal(self, obs):
        """A point comfortably inside the circle we must reach next."""
        z = obs["zone"]
        t = obs["t"]
        px, py = self.pos[0], self.pos[1]
        cx, cy = z["center"]
        r = z["radius"]
        nx, ny = z["next_center"]
        nr = z["next_radius"]
        ss = z.get("shrink_start", 1e9)
        # stay ahead of a shrink once travelling would be tight
        # always pre-position into the next circle; camping on the rim loses
        # the supply drops and the final HP race
        use_next = nr < r - 1.0
        tc = (nx, ny) if use_next else (cx, cy)
        tr = nr if use_next else r
        if tr <= 1.0:
            return tc[0], tc[1]
        # aim well inside, not at the boundary (avoids flip-flopping there)
        depth = min(max(tr * 0.35, 9.0), 32.0)
        dx, dy = px - tc[0], py - tc[1]
        d = math.hypot(dx, dy)
        if d <= tr - depth:
            return px, py
        ux, uy = unit(dx, dy)
        edge = max(tr - depth, 0.0)
        return tc[0] + ux * edge, tc[1] + uy * edge

    def _zone_urgent(self, obs, zgx, zgy):
        z = obs["zone"]
        dps = z.get("dps", 20)
        cx, cy = z["center"]
        margin = z["radius"] - math.hypot(cx - self.pos[0], cy - self.pos[1])
        if not self.in_zone or margin < 2.0:
            self.zone_forced = True
        elif margin > 28.0:
            self.zone_forced = False
        if self.zone_forced:
            return True
        d = math.hypot(zgx - self.pos[0], zgy - self.pos[1])
        return d > 60 and dps >= 100

    # -------------------------------------------------------------- cover
    def _pick_hidden(self, gx, gy, threats, radius, min_dist=3.0,
                     menace=0.0, corridor=False, near_ok=0.0, nearest=False):
        """Best hidden site within `radius`, biased toward the zone goal."""
        px, py = self.pos[0], self.pos[1]
        cands = self.near_sites(px, py, radius)
        if not cands:
            return None
        scored = []
        for s in cands:
            d = math.hypot(s.x - px, s.y - py)
            if d < min_dist or d > radius:
                continue
            prog = math.hypot(px - gx, py - gy) - math.hypot(s.x - gx, s.y - gy)
            if nearest:
                # escaping: time to cover is everything
                base = -d * 1.6 + prog * 0.15
            else:
                base = prog * 0.8 - d * 0.65 + self._concealment(s) * 0.55
            if near_ok and d < near_ok:
                base += 3.0
            scored.append((base, d, s))
        scored.sort(key=lambda c: -c[0])
        best = None
        bestscore = -INF
        seen = 0
        for base, d, s in scored[:16]:
            seen += 1
            if seen > 3 and self._late(0.026):
                break
            if not self._hidden(s.x, s.y, threats, s.z):
                continue
            nd0 = min((math.hypot(e["pos"][0] - px, e["pos"][1] - py)
                       for e in threats), default=999.0)
            nd1 = min((math.hypot(e["pos"][0] - s.x, e["pos"][1] - s.y)
                       for e in threats), default=999.0)
            score = base * 1.0 + (nd1 - nd0) * 0.9
            if menace:
                # do not run toward the threat for a marginal spot
                score -= max(0.0, nd0 - nd1) * menace
            if corridor and not self._corridor_hidden(px, py, s.x, s.y, threats):
                score -= 8.0
            if score > bestscore:
                bestscore = score
                best = s
        return best

    def _nearest_hidden_patch(self, threats, max_r=52.0):
        """Closest walkable point with zero exposure; rings, fine angles."""
        a = self.arena
        px, py, pz = self.pos[0], self.pos[1], self.pos[2]
        nearest = threats[0] if threats else None
        t_ang = math.atan2(nearest["pos"][1] - py, nearest["pos"][0] - px) if nearest else 0.0
        last = self.prev_move
        last_ang = math.atan2(last[1], last[0]) if (last[0] or last[1]) else t_ang + math.pi
        b = a.bounds
        best = None
        bestscore = INF
        radius = 3.0
        while radius <= max_r:
            if self._late(0.028):
                return best
            steps = 24 if radius < 16.0 else 36
            for k in range(steps):
                if k % 8 == 0 and self._late(0.028):
                    return best
                ang = k * TAU / steps
                dx, dy = math.cos(ang), math.sin(ang)
                x = px + dx * radius
                y = py + dy * radius
                ok, z = a.walkable(x, y, pz)
                if not ok:
                    continue
                if not self._hidden(x, y, threats, z):
                    continue
                # are we a few metres *inside* the shadow, or on its knife edge?
                gx2, gy2 = px + dx * (radius + 4.0), py + dy * (radius + 4.0)
                ok2, z2 = a.walkable(gx2, gy2, z)
                deep = ok2 and self._hidden(gx2, gy2, threats, z2)
                # prefer near, not into the hunter's face, and keep going
                toward = max(0.0, math.cos(ang - t_ang))
                turn = 1.0 - math.cos(ang - last_ang)
                edge = min(x - b[0], b[1] - x, y - b[2], b[3] - y)
                score = (radius + toward * 9.0 + turn * 2.5
                         - (5.0 if deep else 0.0)
                         + (max(0.0, 14.0 - edge) * 1.5))
                if score < bestscore:
                    bestscore = score
                    best = (gx2, gy2) if deep else (x, y)
            if best is not None and radius >= 11.0:
                return best
            radius += 2.5 if radius < 16.0 else 5.0
        return best

    def _escape_goal(self, threats, gx, gy, urgent=False):
        """Nearest point that breaks line of sight to every threat."""
        px, py = self.pos[0], self.pos[1]
        # 1. the closest patch of hidden ground: fewest steps under fire
        p = self._nearest_hidden_patch(threats, 55.0 if not urgent else 40.0)
        if p is not None:
            return p
        # 2. a building face a little further out
        s = self._pick_hidden(gx, gy, threats, 95.0, min_dist=2.0,
                              menace=1.4, corridor=False, nearest=True)
        if s is not None:
            return (s.x, s.y)
        # last resort: run away from the threats, toward the zone a little
        rx = ry = 0.0
        for e in threats:
            ux, uy = unit(px - e["pos"][0], py - e["pos"][1])
            rx += ux * e["w"]
            ry += uy * e["w"]
        ux, uy = unit(rx + (gx - px) * 0.01, ry + (gy - py) * 0.01)
        if ux == 0.0 and uy == 0.0:
            ux, uy = unit(gx - px, gy - py)
        return (px + ux * 26.0, py + uy * 26.0)

    def _covered_step(self, tx, ty, threats, now, radius=60.0):
        """One hidden hop toward (tx, ty); None if no covered hop exists."""
        if (self.loot_step is not None and now - self.loot_step_t < 1.2
                and math.hypot(self.loot_step[0] - self.pos[0],
                               self.loot_step[1] - self.pos[1]) > 3.0):
            return self.loot_step
        s = self._pick_hidden(tx, ty, threats, radius, min_dist=5.0,
                              menace=0.5, corridor=True)
        if s is None:
            return None
        self.loot_step = (s.x, s.y)
        self.loot_step_t = now
        return self.loot_step

    def _hide_goal(self, threats, gx, gy, now):
        """While hidden: hold, or relocate along covered corridors."""
        px, py = self.pos[0], self.pos[1]
        s = self.anchor
        if s is not None:
            d = math.hypot(s.x - px, s.y - py)
            if d > 3.5 and now < self.anchor_t + 9.0:
                if self._hidden(s.x, s.y, threats, s.z):
                    return (s.x, s.y)
            if d <= 3.5 and now < self.anchor_t + self.anchor_hold:
                return None  # hold still, stay quiet
        # choose a new covered spot, preferably away from the nearest hunter
        if s is not None:
            gx = px + (gx - px) * 0.4
            gy = py + (gy - py) * 0.4
        ns = self._pick_hidden(gx, gy, threats, 58.0, min_dist=9.0,
                               menace=0.8, corridor=True, near_ok=0.0)
        if ns is None:
            # no fully covered hop: take a hidden site even if the walk is not
            ns = self._pick_hidden(gx, gy, threats, 72.0, min_dist=9.0,
                                   menace=0.5, corridor=False)
        if ns is None:
            # still nothing: never stand still next to a known hunter
            cands = self.near_sites(px, py, 65.0)
            best = None
            bestscore = -INF
            for cand in cands:
                d = math.hypot(cand.x - px, cand.y - py)
                if d < 10.0:
                    continue
                nd = min((math.hypot(cand.x - e["pos"][0], cand.y - e["pos"][1])
                          for e in threats), default=999.0)
                sc = nd - 0.4 * d
                if sc > bestscore:
                    bestscore = sc
                    best = cand
            if best is not None:
                ns = best
        if ns is None:
            # last resort: back away from the nearest threat
            e = threats[0]
            ux, uy = unit(px - e["pos"][0], py - e["pos"][1])
            if ux == 0.0 and uy == 0.0:
                ux, uy = math.cos(now), math.sin(now)
            self.anchor = None
            return (px + ux * 18.0, py + uy * 18.0)
        self.anchor = ns
        self.anchor_t = now
        self.anchor_hold = 0.8 + self.rng.random() * 1.4
        return (ns.x, ns.y)

    def _remember_resources(self, obs):
        """Static loot stays useful after the torso turns away."""
        t = obs["t"]
        px, py = self.pos[0], self.pos[1]
        seen = set()
        for field, kind in (("crates", "crate"), ("cubes", "cube"),
                            ("pickups", "pickup")):
            for o in obs.get(field, ()):
                p = o.get("pos")
                if not p:
                    continue
                self.res_mem[o["id"]] = (kind, p[0], p[1], p[2], t)
                seen.add(o["id"])
        for ident, (kind, x, y, z, ts) in list(self.res_mem.items()):
            reached = (kind == "cube" and math.hypot(x - px, y - py) < 5.0
                       and ident not in seen)
            gone = t - ts > 75.0
            if reached or gone:
                del self.res_mem[ident]

    def _pick_loot(self, obs, threats, nd, now, exp_here, can_move):
        """Power cubes are max HP, and max HP decides the final collapse."""
        px, py = self.pos[0], self.pos[1]
        if self.loot is not None:
            dead = self.loot_dead.get(self.loot[3], now + 30.0)
            if now > dead:
                self.loot_black[self.loot[3]] = now + 35.0
                self.loot = None
        z = obs["zone"]
        c = z["center"]
        r = z["radius"]
        nc = z["next_center"]
        nr = z.get("next_radius", 0.0)

        def in_zone(x, y):
            if nr > 1.0 and math.hypot(x - nc[0], y - nc[1]) <= nr - 5.0:
                return True
            return math.hypot(x - c[0], y - c[1]) <= max(r - 6.0, 4.0)

        # ---- crates: only from cover, and only with some breathing room
        ca = None
        if ((exp_here <= 0.0 or (nd > 150.0 and self.hp > 0.65 * self.max_hp))
                and (not threats or nd > 95.0)):
            cad = 1e9
            muzzle = (px, py, self.pos[2] + self.muzzle_h)
            for ident, (kind, x, y, zc, ts) in self.res_mem.items():
                if kind != "crate":
                    continue
                dd = math.hypot(x - px, y - py)
                if dd > 70.0 or dd > cad or not in_zone(x, y):
                    continue
                if not self.arena.segment_blocked(muzzle, (x, y, zc + 1.1),
                                                  terrain=False):
                    cad = dd
                    ca = (x, y, zc + 1.1)
        # drones drop two cubes each; they are easy and do not count for placement
        if ca is None and (not threats or nd > 170.0):
            for m in obs.get("mechs", ()):
                if m.get("kind") != "npc":
                    continue
                x, y, zc = m["pos"]
                dd = math.hypot(x - px, y - py)
                if dd > 50.0 or m.get("hp", 999.0) > 360.0:
                    continue
                ca = (x, y, zc + 1.5)
                break
        self.crate_aim = ca

        # a supply drop is the jackpot: three cubes plus a full heal.
        # It is worth a covered approach even while hiding.
        drop = None
        bd = 1e9
        for d in obs.get("drops", []):
            if d.get("lands_in", 0.0) > 26.0 or d.get("holder") is not None:
                continue
            x, y = d["pos"][0], d["pos"][1]
            dd = math.hypot(x - px, y - py)
            did = d["id"]
            if (dd > 155.0 or not in_zone(x, y)
                    or self.loot_black.get(did, 0.0) > now):
                continue
            near = min((math.hypot(e["pos"][0] - x, e["pos"][1] - y)
                        for e in threats), default=999.0)
            if near < 120.0:
                continue
            if dd < bd:
                bd = dd
                drop = (x, y, "drop", did)

        if not can_move:
            if drop is not None:
                did = drop[3]
                if did not in self.loot_dead:
                    self.loot_dead[did] = now + 20.0
                if now <= self.loot_dead[did]:
                    self.loot = drop
                    self.loot_t = now
                else:
                    self.loot_black[did] = now + 35.0
                return
            # grab a cube if we can reach it without breaking cover
            best = None
            bd = 115.0 if self.cubes < 3 else 82.0
            for ident, (kind, x, y, zc, ts) in self.res_mem.items():
                if kind != "cube":
                    continue
                dd = math.hypot(x - px, y - py)
                if (dd < bd and in_zone(x, y)
                        and self.loot_black.get(ident, 0.0) <= now):
                    bd = dd
                    best = (x, y, "cube", ident)
            if best is None and self.cubes < 8:
                bd = 1e9
                for ident, (kind, x, y, zc, ts) in self.res_mem.items():
                    if kind != "crate" or self.loot_black.get(ident, 0.0) > now:
                        continue
                    dd = math.hypot(x - px, y - py)
                    if dd > 85.0 or not in_zone(x, y):
                        continue
                    if dd - 18.0 < bd:
                        bd = dd - 18.0
                        ux, uy = unit(px - x, py - y)
                        best = (x + ux * 22.0, y + uy * 22.0, "crate", ident)
            if best is not None:
                bid = best[3]
                if bid not in self.loot_dead:
                    self.loot_dead[bid] = now + 13.0
                if now <= self.loot_dead[bid]:
                    self.loot = best
                    self.loot_t = now
            elif self.loot is not None and (now - self.loot_t > 4.0
                                           or math.hypot(self.loot[0] - px,
                                                         self.loot[1] - py) < 2.5):
                self.loot = None
            return

        cur = self.loot
        if cur is not None:
            if (now - self.loot_t < 5.0 and in_zone(cur[0], cur[1])
                    and math.hypot(cur[0] - px, cur[1] - py) > 2.5):
                return
            self.loot = None

        best = drop
        if best is None:
            bd = 1e9
            for ident, (kind, x, y, zc, ts) in self.res_mem.items():
                if self.loot_black.get(ident, 0.0) > now:
                    continue
                if kind == "cube":
                    dd = math.hypot(x - px, y - py)
                    if (dd > (115.0 if self.cubes < 3 else 82.0)
                            or not in_zone(x, y)):
                        continue
                    if dd < bd:
                        bd = dd
                        best = (x, y, "cube", ident)
                elif kind == "crate" and self.cubes < 8:
                    dd = math.hypot(x - px, y - py)
                    if (dd > (95.0 if self.cubes < 3 else 70.0)
                            or not in_zone(x, y)):
                        continue
                    if dd - 18.0 < bd:
                        bd = dd - 18.0
                        ux, uy = unit(px - x, py - y)
                        best = (x + ux * 22.0, y + uy * 22.0, "crate", ident)
        if best is not None:
            bid = best[3]
            if bid not in self.loot_dead:
                self.loot_dead[bid] = now + (20.0 if best[2] == "drop" else 13.0)
            elif now > self.loot_dead[bid]:
                self.loot_black[bid] = now + 35.0
                best = None
        if best is not None:
            self.loot = best
            self.loot_t = now

    def _roam_goal(self, gx, gy, now):
        """No hunter known: settle in cover, occasionally move on."""
        px, py = self.pos[0], self.pos[1]
        if self.anchor is not None:
            d = math.hypot(self.anchor.x - px, self.anchor.y - py)
            if d > 3.5:
                return (self.anchor.x, self.anchor.y)
            if now < self.anchor_t + self.anchor_hold:
                return None
        # far from the zone: head that way; otherwise patrol locally
        dz = math.hypot(px - gx, py - gy)
        if dz > 22.0:
            tx, ty = gx, gy
            rad = min(60.0, dz + 20.0)
            hold = 2.0 + self.rng.random() * 3.0
        else:
            a = self.rng.random() * TAU
            rr = 18.0 + self.rng.random() * 30.0
            tx = px + math.cos(a) * rr
            ty = py + math.sin(a) * rr
            rad = 48.0
            hold = 4.0 + self.rng.random() * 5.0
        cands = self.near_sites(px, py, rad)
        best = None
        bd = INF
        for s in cands:
            d = math.hypot(s.x - px, s.y - py)
            if d < 9.0:
                continue
            # near the desired direction, and tucked between buildings
            score = (d + 0.75 * math.hypot(s.x - tx, s.y - ty)
                     - self._concealment(s) * 3.2)
            if score < bd:
                bd = score
                best = s
        if best is None:
            # no mapped face nearby: at least keep closing on the zone
            return (gx, gy) if dz > 10.0 else None
        self.anchor = best
        self.anchor_t = now
        self.anchor_hold = hold
        return (best.x, best.y)

    # ------------------------------------------------------------ steering
    def _passable(self, x, y, z):
        """Walkable, allowing a jumpable step."""
        ok, h = self.arena.walkable(x, y, z)
        if ok:
            return True
        if 0.45 < h - z < 2.4:
            self.want_jump = True
            return True
        return False

    def _clear_ahead(self, ang, dist):
        dx, dy = math.cos(ang), math.sin(ang)
        x, y, z = self.pos[0], self.pos[1], self.pos[2]
        for f in (0.45, 1.0):
            px = x + dx * dist * f
            py = y + dy * dist * f
            if not self._passable(px, py, z):
                return False
            # lateral clearance, so a wall edge does not catch the hull
            ox, oy = -dy * 2.4, dx * 2.4
            if f > 0.9:
                if not self._passable(px + ox, py + oy, z):
                    return False
                if not self._passable(px - ox, py - oy, z):
                    return False
        return True

    def _steer(self, desired):
        """Turn a heading into the closest one that is actually walkable."""
        if self._clear_ahead(desired, 4.5):
            self.turn_bias = 0.0
            return desired
        # keep the wall on the same side rather than dithering at a corner
        last = self.prev_move
        last_ang = math.atan2(last[1], last[0]) if (last[0] or last[1]) else desired
        best = None
        bestscore = INF
        for off in STEER_OFFSETS:
            ang = desired + off
            if not self._clear_ahead(ang, 4.0):
                continue
            score = abs(ang_diff(ang, last_ang)) + abs(off) * 0.35
            if score < bestscore:
                bestscore = score
                best = ang
        if best is not None:
            self.turn_bias = ang_diff(best, desired)
            return best
        return desired

    def _drive(self, goal, now, boost):
        """Walk toward a world point, following the cached route."""
        if goal is None:
            return 0.0, 0.0, boost
        gx, gy = goal
        x, y = self.pos[0], self.pos[1]
        d = math.hypot(gx - x, gy - y)
        if d < 0.25:
            return 0.0, 0.0, boost
        escape = self.goal_kind in ("break", "flee", "dodge")
        if (d < 7.0 and self._clear_line(x, y, gx, gy)) or (escape and d < 26.0):
            wx, wy = gx, gy
        elif self._late(0.032):
            wx, wy = gx, gy
        else:
            wx, wy = self._waypoint(gx, gy, now)
        ang = math.atan2(wy - y, wx - x)
        ang = self._steer(ang)
        return math.cos(ang), math.sin(ang), boost

    # ------------------------------------------------------------ dodging
    def _dodge(self, obs):
        px, py, pz = self.pos
        best = None
        bw = 0.0
        urgent = False
        for pr in obs.get("projectiles", []):
            x, y, z = pr["pos"]
            vx, vy, vz = pr["vel"]
            if pr.get("kind") == "mortar":
                # ballistic landing point
                g = 22.0
                disc = vz * vz - 2.0 * g * (z - pz)
                if disc < 0.0:
                    continue
                t = (vz + math.sqrt(disc)) / g
                if not (0.0 < t < 3.6):
                    continue
                lx = x + vx * t
                ly = y + vy * t
                d = math.hypot(lx - px, ly - py)
                if d > 11.0:
                    continue
                ux, uy = unit(px - lx, py - ly)
                if ux == 0.0 and uy == 0.0:
                    ux, uy = unit(-vy, vx)
                w = (11.0 - d) / 11.0
                if w > bw:
                    bw = w
                    best = (ux, uy)
                    urgent = True
                continue
            rx = px - x
            ry = py - y
            vv = vx * vx + vy * vy
            if vv < 1.0:
                continue
            tca = (rx * vx + ry * vy) / vv
            if tca < 0.0 or tca > 0.6:
                continue
            mx = rx + vx * tca
            my = ry + vy * tca
            miss = math.hypot(mx, my)
            if miss > 7.0:
                continue
            nx, ny = unit(-vy, vx)
            if nx * mx + ny * my < 0:
                nx, ny = -nx, -ny
            w = (7.0 - miss) / 7.0
            if w > bw:
                bw = w
                best = (nx, ny)
        self.mortar_danger = urgent
        return best

    # ----------------------------------------------------------------- act
    def act(self, obs):
        self._t0 = time.perf_counter()
        self._exp_cache = {}
        t = obs["t"]
        me = obs["self"]
        self.t = t
        self.pos = list(me["pos"])
        self.vel = list(me.get("vel", [0, 0, 0]))
        self.aim = list(me.get("aim", [0.0, 0.0]))
        self.hp = me["hp"]
        self.max_hp = me["max_hp"]
        self.energy = me["energy"]
        self.grounded = me.get("grounded", True)
        self.in_zone = me.get("in_zone", True)
        self.cubes = me.get("cubes", 0)
        self.weapons = me.get("weapons", [None, None])
        stats = me.get("stats", {})
        self.walk = stats.get("walk_speed", self.walk)
        muzzle = stats.get("muzzle_height", self.muzzle_h)
        self.want_jump = False

        x, y, z = self.pos

        # ---- events -----------------------------------------------------
        for ev in obs.get("events", []):
            et = ev.get("type")
            if et == "damaged":
                self.last_damage_t = t
                b = ev.get("bearing")
                if b is not None:
                    self.last_damage_bearing = b
                by = ev.get("by")
                if by is not None:
                    bb = b if b is not None else 0.0
                    self.known[by] = {
                        "pos": [x + math.cos(bb) * 110.0, y + math.sin(bb) * 110.0,
                                self.arena.height_fast(x, y)],
                        "vel": [0, 0, 0], "t": t, "hp": None,
                        "weapons": [], "charging": True}
            elif et == "effect":
                self.last_damage_t = t

        self._update_threats(obs)
        self._remember_resources(obs)
        threats = self.threats
        nearest = threats[0] if threats else None
        nd = math.hypot(nearest["pos"][0] - x, nearest["pos"][1] - y) if nearest else INF

        gx, gy = self._zone_goal(obs)
        zc = obs["zone"]["center"]
        zr = obs["zone"]["radius"]
        zdist = math.hypot(x - zc[0], y - zc[1])
        margin = zr - zdist
        zdps = obs["zone"].get("dps", 20)
        if margin < 2.0:
            self.zone_forced = True
        elif margin > 28.0:
            self.zone_forced = False
        exp_here = self._exposure(x, y, threats, z)
        urgent = self.charge_threat and exp_here > 0.0 and nd < 320.0
        # a blind dash is only worth it when staying out is certain death
        zone_dash = (margin < -6.0
                     or (zdps >= 60 and margin < 8.0)
                     or (zr <= 30 and margin < 0.0))

        move = [0.0, 0.0]
        boost = False
        jump = False
        mode = "idle"
        goal = None

        # ---- pick a goal ------------------------------------------------
        if zone_dash:
            mode = "zone!"
            if zr <= 1.0:
                goal = (zc[0], zc[1])
            else:
                ux, uy = unit(x - zc[0], y - zc[1])
                depth = 20.0 if zr > 60.0 else max(2.0, zr * 0.35)
                goal = (zc[0] + ux * max(zr - depth, 0.0),
                        zc[1] + uy * max(zr - depth, 0.0))
            boost = self.energy > 5
        elif urgent:
            mode = "break"
            if (self.esc_goal is None
                    or t - self.esc_t > 1.5
                    or math.hypot(self.esc_goal[0] - x, self.esc_goal[1] - y) < 3.0
                    or (t - self.esc_t > 0.5
                        and not self._hidden(self.esc_goal[0], self.esc_goal[1], threats))):
                self.esc_goal = self._escape_goal(threats, gx, gy, urgent=True)
                self.esc_t = t
            goal = self.esc_goal
            boost = True
        elif self.zone_forced:
            # get inside, but hug cover on the way when there is time
            mode = "zone"
            if nearest is not None and nd < 320.0:
                s = self._pick_hidden(gx, gy, threats, 70.0, min_dist=6.0,
                                      corridor=True)
                goal = (s.x, s.y) if s is not None else (gx, gy)
            else:
                goal = (gx, gy)
            boost = math.hypot(goal[0] - x, goal[1] - y) > 20.0 and self.energy > 10
        elif exp_here > 0.0 and nearest is not None and nd < 340.0:
            mode = "flee"
            if (self.esc_goal is None
                    or t - self.esc_t > 1.6
                    or math.hypot(self.esc_goal[0] - x, self.esc_goal[1] - y) < 3.0
                    or (t - self.esc_t > 0.5
                        and not self._hidden(self.esc_goal[0], self.esc_goal[1], threats))):
                self.esc_goal = self._escape_goal(threats, gx, gy, urgent=False)
                self.esc_t = t
            goal = self.esc_goal
            boost = True
        elif self._zone_urgent(obs, gx, gy):
            mode = "zone"
            if nearest is not None and nd < 320.0:
                s = self._pick_hidden(gx, gy, threats, 70.0, min_dist=6.0,
                                      corridor=True)
                goal = (s.x, s.y) if s is not None else (gx, gy)
            else:
                goal = (gx, gy)
            boost = math.hypot(gx - x, gy - y) > 25.0 and self.energy > 15
        elif nearest is not None and nd < 300.0:
            mode = "hide"
            self.esc_goal = None
            self._pick_loot(obs, threats, nd, t, exp_here, False)
            step = None
            if self.loot is not None:
                lx, ly = self.loot[0], self.loot[1]
                ld = math.hypot(lx - x, ly - y)
                if ld < 18.0 and self._corridor_hidden(x, y, lx, ly, threats[:1]):
                    step = (lx, ly)
                elif ld < 210.0 and self.loot[2] == "drop":
                    near = min((math.hypot(e["pos"][0] - lx, e["pos"][1] - ly)
                                for e in threats), default=999.0)
                    if near > 120.0:
                        step = self._covered_step(lx, ly, threats, t)
                elif ld < 110.0 and self.loot[2] == "cube":
                    step = self._covered_step(lx, ly, threats, t, 55.0)
                elif ld < 100.0 and self.loot[2] == "crate":
                    step = self._covered_step(lx, ly, threats, t, 50.0)
            if step is not None:
                mode = "loot"
                goal = step
            else:
                goal = self._hide_goal(threats, gx, gy, t)
        else:
            mode = "roam"
            self.esc_goal = None
            self._pick_loot(obs, threats, nd, t, exp_here, True)
            if self.loot is not None and not self.zone_forced:
                mode = "loot"
                goal = (self.loot[0], self.loot[1])
            else:
                goal = self._roam_goal(gx, gy, t)

        # zone keeps a veto when it is lethal to linger
        if mode in ("hide", "roam") and self._zone_urgent(obs, gx, gy):
            mode = "zone"
            if nearest is not None and nd < 320.0:
                s = self._pick_hidden(gx, gy, threats, 70.0, min_dist=6.0,
                                      corridor=True)
                goal = (s.x, s.y) if s is not None else (gx, gy)
            else:
                goal = (gx, gy)
            boost = self.energy > 15
        if mode not in ("roam", "loot", "hide"):
            self.loot = None
            self.crate_aim = None
        self.goal = goal
        self.goal_kind = mode

        if goal is not None:
            mx, my, boost = self._drive(goal, t, boost)
            move = [mx, my]

        # ---- incoming bullets ------------------------------------------
        dodge = self._dodge(obs)
        if dodge is not None and (mode not in ("break", "flee")
                                  or (self.mortar_danger and mode != "zone!")):
            mode = "dodge"
            move = [dodge[0], dodge[1]]
            goal = None

        # ---- keep the speed up on long runs ----------------------------
        if not boost and self.grounded and self.energy > 22 and goal is not None:
            if math.hypot(goal[0] - x, goal[1] - y) > 30.0:
                boost = True

        # ---- stuck recovery --------------------------------------------
        if self.prev_pos is not None:
            moved = math.hypot(x - self.prev_pos[0], y - self.prev_pos[1])
            if moved < 0.15 and (move[0] or move[1]):
                self.stuck += 1
            else:
                self.stuck = 0
        self.prev_pos = (x, y)
        if self.stuck > 5 and t - self.stuck_t > 0.7:
            self.stuck_t = t
            self.stuck = 0
            self.escape_until = t + 1.1
            base = math.atan2(move[1], move[0]) if (move[0] or move[1]) else self.rng.random() * TAU
            self.escape_dir = base + self.rng.choice((1.0, -1.0)) * self.rng.uniform(2.0, 3.0)
            self.path = []
            self.path_goal = None
        if t < self.escape_until:
            move = [math.cos(self.escape_dir), math.sin(self.escape_dir)]
            jump = True
            boost = True
            mode = "unstick"
            goal = None

        # ---- climbing ---------------------------------------------------
        mag = math.hypot(move[0], move[1])
        if mag > 0.01:
            ax_ = x + move[0] / mag * 2.2
            ay_ = y + move[1] / mag * 2.2
            ha = self.arena.height_fast(ax_, ay_)
            if self.grounded and ha - z > 0.55:
                jump = True
                if ha - z > 2.0:
                    boost = True
        if self.want_jump and self.grounded:
            jump = True
        # never hold boost in the air: that is a jet, and a jammed jet climbs
        # until the match ends (or the map ceiling)
        if not self.grounded:
            boost = False

        # ---- aim / fire --------------------------------------------------
        aim_yaw, aim_pitch = self._aim_target(obs, threats, move, muzzle)
        fire = [False, False]
        self._maybe_fire(obs, aim_yaw, aim_pitch, muzzle, fire)

        extra = {}
        self._maybe_take(obs, extra)
        if obs.get("offer"):
            extra["pick"] = self._pick_upgrade(obs["offer"])

        if mag > 1.0:
            move = [move[0] / mag, move[1] / mag]
        self.prev_move = (move[0], move[1])
        self.mode = mode
        act = {"tick": obs["tick"], "move": [float(move[0]), float(move[1])],
               "boost": bool(boost), "jump": bool(jump),
               "aim": [float(aim_yaw), float(aim_pitch)], "fire": fire}
        act.update(extra)
        return act

    # ------------------------------------------------------------ tactics
    def _aim_target(self, obs, threats, move, muzzle):
        x, y, z = self.pos
        best = None
        bd = 1e9
        for e in threats:
            if not e.get("real"):
                continue
            d = math.hypot(e["pos"][0] - x, e["pos"][1] - y)
            if d < bd:
                bd = d
                best = e
        if best is not None and bd < 300.0:
            yaw = math.atan2(best["pos"][1] - y, best["pos"][0] - x)
            pitch = math.atan2(best["pos"][2] + self.height * 0.5 - (z + muzzle),
                               max(bd, 1.0))
            return yaw, pitch
        if self.crate_aim is not None:
            cx, cy, cz = self.crate_aim
            d = math.hypot(cx - x, cy - y)
            return math.atan2(cy - y, cx - x), math.atan2(cz - (z + muzzle), max(d, 1.0))
        # no target: scan slowly so hunters are spotted before they close in
        age = obs["t"] - self.last_damage_t
        if age > 4.0 and not threats:
            yaw = self.aim[0] + 0.9
            if yaw > math.pi:
                yaw -= TAU
            return yaw, -0.05
        if self.last_damage_bearing is not None and age < 3.0:
            return self.last_damage_bearing, -0.02
        yaw = math.atan2(move[1], move[0]) if (move[0] or move[1]) else self.aim[0]
        return yaw, -0.06

    def _maybe_fire(self, obs, aim_yaw, aim_pitch, muzzle, fire):
        x, y, z = self.pos
        gun = self.weapons[0]
        if gun is not None and gun.get("cooldown", 0.0) <= 0.02 and gun.get("ammo", 1) > 0:
            gs = gun.get("stats", self.gun)
            gspeed = gs.get("speed", self.gun["speed"])
            grange = gs.get("range", self.gun["range"])
            target = None
            bd = 1e9
            for m in obs["mechs"]:
                if m["kind"] != "mech":
                    continue
                d = math.hypot(m["pos"][0] - x, m["pos"][1] - y)
                if d < bd and d < grange * 0.95:
                    bd = d
                    target = m
            if target is not None:
                tt = bd / max(gspeed, 1.0)
                tp = target["pos"]
                tv = target.get("vel", [0, 0, 0])
                lx = tp[0] + tv[0] * tt
                ly = tp[1] + tv[1] * tt
                lz = tp[2] + tv[2] * tt + self.height * 0.5
                dyaw = math.atan2(ly - y, lx - x)
                dpitch = math.atan2(lz - (z + muzzle), max(math.hypot(lx - x, ly - y), 1.0))
                if abs(ang_diff(self.aim[0], dyaw)) < 0.055 and abs(self.aim[1] - dpitch) < 0.1:
                    mp = (x + math.cos(self.aim[0]) * 3.0, y + math.sin(self.aim[0]) * 3.0,
                          z + muzzle)
                    if not self.arena.segment_blocked(mp, (lx, ly, lz), terrain=False):
                        fire[0] = True
            elif self.crate_aim is not None:
                cx, cy, cz = self.crate_aim
                dyaw = math.atan2(cy - y, cx - x)
                dpitch = math.atan2(cz - (z + muzzle),
                                    max(math.hypot(cx - x, cy - y), 1.0))
                if (abs(ang_diff(self.aim[0], dyaw)) < 0.06
                        and abs(self.aim[1] - dpitch) < 0.12):
                    mp = (x + math.cos(self.aim[0]) * 3.0, y + math.sin(self.aim[0]) * 3.0,
                          z + muzzle)
                    if not self.arena.segment_blocked(mp, (cx, cy, cz), terrain=False):
                        fire[0] = True
        w1 = self.weapons[1]
        if w1 is not None and w1.get("kind") == "lightning" and w1.get("cooldown", 0) <= 0.02:
            if self.energy > 8:
                for m in obs["mechs"]:
                    if m["kind"] != "mech":
                        continue
                    d = math.hypot(m["pos"][0] - x, m["pos"][1] - y)
                    if d < self.lightning["range"] * 0.9:
                        fire[1] = True
                        break

    def _maybe_take(self, obs, extra):
        if self.weapons[1] is not None:
            return
        x, y, z = self.pos
        held = [w["kind"] for w in self.weapons if w]
        for p in obs.get("pickups", []):
            if p["kind"] == "gun" or p["kind"] in held:
                continue
            d = math.hypot(p["pos"][0] - x, p["pos"][1] - y)
            if d < self.loot_r - 0.4 and abs(p["pos"][2] - z) < 3.0:
                extra["take"] = p["id"]
                return

    def _pick_upgrade(self, offer):
        rank = {"armor": 10, "plating": 9, "nanites": 8, "servos": 7,
                "jets": 6, "capacitor": 5, "reactor": 5, "sensors": 4,
                "gun_damage": 3, "gun_rate": 3, "gun_mag": 2, "gun_velocity": 2,
                "lightning_damage": 3}
        best = 0
        bs = -1
        for i, c in enumerate(offer.get("choices", [])):
            s = rank.get(c.get("id", ""), 1)
            if s > bs:
                bs = s
                best = i
        return best


if __name__ == "__main__":
    mf.run(Bot)
