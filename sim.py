"""Offline testbed: a lightweight match simulator for the Ghost bot.

This is *not* the official server.  It reproduces the rules that matter for
survival (movement, walls, zone, vision, the gun and the laser) closely enough
to tell whether a strategy works, and it drives a few plausible house-bot
personalities.  It is a development tool only -- never imported by the bot.

    python3 sim.py            # a batch of seeded matches
    python3 sim.py -v 3       # verbose, seed 3
"""
import math
import random
import sys

from arena import Arena

TAU = 2 * math.pi


def ang_diff(a, b):
    return (a - b + math.pi) % TAU - math.pi


def unit(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-9 else (0.0, 0.0)


CFG = {
    "mech": {"radius": 2.5, "height": 9, "eye_height": 8, "muzzle_height": 7,
             "hp": 1400, "walk_speed": 11.7, "boost_speed": 19.5,
             "ground_accel": 58.5, "air_accel": 12, "gravity": 22,
             "jump_speed": 11, "jet_accel": 34, "max_rise_speed": 10,
             "step_up": 1.2, "max_slope_deg": 50, "energy": 100,
             "energy_regen": 18, "energy_regen_delay": 0.6,
             "boost_drain": 17, "jet_drain": 15, "turn_rate": 4,
             "pitch_rate": 3, "min_pitch": -0.7, "max_pitch": 1.3,
             "regen_delay": 5, "regen_per_s": 45, "armor": 0,
             "rear_mult": 1.5, "front_arc_deg": 180},
    "vision": {"range": 200, "fov_deg": 120, "near_range": 45, "memory_s": 5},
    "sound": {"bearing_noise_deg": 7, "near": 60, "mid": 160,
              "range": {"gun": 320, "laser": 380, "laser_charge": 140,
                        "lightning": 160, "jet": 90, "boost": 90,
                        "step": 40, "reload": 60, "explosion": 420,
                        "mortar": 420, "death": 600}},
    "weapons": {"gun": {"damage": 45, "cooldown": 0.24, "speed": 252,
                        "range": 230, "spread_deg": 0.6, "mag": 12, "reload": 2.4},
                "laser": {"damage": 315, "charge": 0.75, "min_charge": 0.15,
                          "min_frac": 0.35, "hold_s": 2, "cooldown": 1.65,
                          "range": 280, "charge_turn_mult": 1},
                "lightning": {"damage": 60, "cooldown": 0.25, "range": 45,
                              "cone_deg": 30, "energy": 3},
                "mortar": {"damage": 200, "splash": 160, "splash_radius": 7,
                           "cooldown": 2.6, "speed": 72, "gravity": 22}},
    "zone": {"start_center": [41.3, -72.8], "start_radius": 560,
             "phases": [{"hold_s": 60, "shrink_s": 50, "radius": 380, "dps": 20},
                        {"hold_s": 45, "shrink_s": 40, "radius": 240, "dps": 35},
                        {"hold_s": 35, "shrink_s": 35, "radius": 140, "dps": 60},
                        {"hold_s": 30, "shrink_s": 30, "radius": 70, "dps": 100},
                        {"hold_s": 25, "shrink_s": 25, "radius": 25, "dps": 160},
                        {"hold_s": 15, "shrink_s": 25, "radius": 0, "dps": 260}]},
    "cubes": {"pickup_radius": 3.5},
    "loot": {"pickup_radius": 4},
    "npc": {"count": 0},
    "drops": {"times": [70, 140, 210], "fall_s": 25, "radius": 8,
              "capture_s": 4, "cubes": 3, "repair": 1, "spread": 0.7},
    "crates": {"count": 0},
    "debug": {},
    "upgrades": [],
    "match": {"duration_s": 480},
    "bot": {"tick_budget_ms": 50},
}


class ZoneState:
    def __init__(self, rng):
        self.t0 = 0.0
        self.center = list(CFG["zone"]["start_center"])
        self.radius = CFG["zone"]["start_radius"]
        self.phases = CFG["zone"]["phases"]
        self.phase = -1
        self.next_center = None
        self.next_radius = None
        self.shrink_start = 0.0
        self.shrink_end = 0.0
        self.dps = 0.0
        self._schedule(rng)

    def _schedule(self, rng):
        # centre of the next circle is uniform inside the current one
        t = 0.0
        c = list(self.center)
        r = self.radius
        self.plan = []
        for ph in self.phases:
            maxoff = max(0.0, r - ph["radius"])
            a = rng.uniform(0, TAU)
            off = rng.uniform(0, maxoff)
            nc = [c[0] + math.cos(a) * off, c[1] + math.sin(a) * off]
            self.plan.append({"start": t, "hold": ph["hold_s"], "shrink": ph["shrink_s"],
                              "from": list(c), "fr": r, "to": nc, "tr": ph["radius"],
                              "dps": ph["dps"]})
            t += ph["hold_s"] + ph["shrink_s"]
            c, r = nc, ph["radius"]

    def at(self, t):
        if not self.plan:
            return
        ph = self.plan[0]
        for p in self.plan:
            if t >= p["start"]:
                ph = p
        hold_end = ph["start"] + ph["hold"]
        if t < hold_end:
            self.center = list(ph["from"])
            self.radius = ph["fr"]
            self.dps = ph["dps"]
            self.shrink_start = hold_end
            self.shrink_end = hold_end + ph["shrink"]
        else:
            f = min(1.0, (t - hold_end) / max(ph["shrink"], 1e-6))
            self.center = [ph["from"][0] + (ph["to"][0] - ph["from"][0]) * f,
                           ph["from"][1] + (ph["to"][1] - ph["from"][1]) * f]
            self.radius = ph["fr"] + (ph["tr"] - ph["fr"]) * f
            self.dps = ph["dps"]
            self.shrink_start = hold_end
            self.shrink_end = hold_end + ph["shrink"]
        idx = self.plan.index(ph)
        # the next circle is the one this phase shrinks to
        self.next_center = list(ph["to"])
        self.next_radius = ph["tr"]


class Mech:
    def __init__(self, mid, pos, hp=1400):
        m = CFG["mech"]
        self.id = mid
        self.pos = [pos[0], pos[1], pos[2]]
        self.vel = [0.0, 0.0, 0.0]
        self.aim = [0.0, 0.0]
        self.hp = hp
        self.max_hp = hp
        self.energy = m["energy"]
        self.grounded = True
        self.cubes = 0
        self.alive = True
        self.gun_cd = 0.0
        self.ammo = CFG["weapons"]["gun"]["mag"]
        self.reload = 0.0
        self.laser_cd = 0.0
        self.charge = 0.0
        self.charging = False
        self.last_damage = -99.0
        self.deaths = 0
        self.hit_flash = -99.0
        self.weapons = ["gun"]
        self.armor = 0.0


class Match:
    def __init__(self, arena, seed, n_hunters=3, tiers=None, spawn=None,
                 log=None, start_t=0.0):
        self.a = arena
        self.rng = random.Random(seed)
        self.t = 0.0
        self.zone = ZoneState(self.rng)
        self.zone.at(0.0)
        self.bullets = []
        self.events = []
        self.bot_log = log
        self.mem = {}
        self.pmem = {}
        self.hunters = []
        self.player_damage = 0.0
        self.player_damage_by = {}
        if spawn is None:
            spawn = self._random_spawn()
        self.player = Mech(0, spawn)
        self.player.upgrades = []
        self.player.weapons = ["gun"]
        self.tiers = tiers or (["sniper"] * n_hunters)
        self._spawn_hunters(n_hunters)
        self.cubes = []
        self.time_alive = 0.0
        self.zone_damage = 0.0
        self.max_exposed = 0.0

    def _random_spawn(self):
        for _ in range(500):
            a = self.rng.uniform(0, TAU)
            r = self.rng.uniform(150, 430)
            x = self.zone.center[0] + math.cos(a) * r
            y = self.zone.center[1] + math.sin(a) * r
            b = self.a.bounds
            if not (b[0] + 20 < x < b[1] - 20 and b[2] + 20 < y < b[3] - 20):
                continue
            ok, h = self.a.walkable(x, y, self.a.height_fast(x, y))
            if ok:
                return [x, y, h]
        return [0.0, 0.0, self.a.height_fast(0, 0)]

    def _spawn_hunters(self, n):
        for i in range(n):
            tier = self.tiers[i % len(self.tiers)]
            for _ in range(200):
                x, y = self._random_spawn()[:2]
                if math.hypot(x - self.player.pos[0], y - self.player.pos[1]) > 130:
                    break
            h = Mech(10 + i, [x, y, self.a.height_fast(x, y)])
            if tier in ("sniper", "expert"):
                h.weapons = ["gun", "laser"]
            elif tier == "rusher":
                h.weapons = ["gun", "lightning"]
            else:
                h.weapons = ["gun"]
            h.tier = tier
            h.strafe = 1 if self.rng.random() < 0.5 else -1
            h.repath = 0.0
            h.dest = None
            self.hunters.append(h)

    # ------------------------------------------------------------- physics
    def step_mech(self, m, move, boost, jump, dt):
        cfg = CFG["mech"]
        mx, my = move
        mag = math.hypot(mx, my)
        if mag > 1.0:
            mx, my = mx / mag, my / mag
            mag = 1.0
        grounded = m.grounded
        if grounded:
            spd = cfg["boost_speed"] if boost and m.energy > 0 else cfg["walk_speed"]
            accel = cfg["ground_accel"]
            vx_des = mx * spd
            vy_des = my * spd
            if boost and mag > 0.05 and m.energy > 0:
                m.energy = max(0.0, m.energy - cfg["boost_drain"] * dt)
        else:
            accel = cfg["air_accel"]
            vx_des = mx * cfg["walk_speed"]
            vy_des = my * cfg["walk_speed"]
            if boost and m.energy > 0:
                m.vel[2] = min(cfg["max_rise_speed"], m.vel[2] + cfg["jet_accel"] * dt)
                m.energy = max(0.0, m.energy - cfg["jet_drain"] * dt)
        for k, des in ((0, vx_des), (1, vy_des)):
            d = des - m.vel[k]
            lim = accel * dt
            m.vel[k] += max(-lim, min(lim, d))
        if jump and grounded:
            m.vel[2] = cfg["jump_speed"]
            m.grounded = False
        if not m.grounded:
            m.vel[2] -= cfg["gravity"] * dt
        # horizontal move with slide
        x, y, z = m.pos
        nx = x + m.vel[0] * dt
        ny = y + m.vel[1] * dt
        nz = z + m.vel[2] * dt
        ok, h = self.a.walkable(nx, ny, z)
        if ok:
            if m.grounded and h < z - 0.6:
                # walked off a ledge
                m.grounded = False
                m.vel[2] = 0.0
            m.pos[0] = nx
            m.pos[1] = ny
            if m.grounded:
                m.pos[2] = h
        else:
            okx, hx = self.a.walkable(nx, y, z)
            oky, hy = self.a.walkable(x, ny, z)
            if okx:
                m.pos[0] = nx
                m.vel[1] = 0.0
                if m.grounded:
                    m.pos[2] = hx
            elif oky:
                m.pos[1] = ny
                m.vel[0] = 0.0
                if m.grounded:
                    m.pos[2] = hy
            else:
                m.vel[0] = 0.0
                m.vel[1] = 0.0
        # vertical
        if not m.grounded:
            ground = self.a.height_fast(m.pos[0], m.pos[1])
            m.pos[2] = nz
            if m.pos[2] <= ground:
                m.pos[2] = ground
                m.vel[2] = 0.0
                m.grounded = True
            elif m.pos[2] > ground + 0.2:
                m.grounded = False
        # regen / energy
        if m.energy < cfg["energy"]:
            if self.t - m.last_damage > cfg["energy_regen_delay"] or m.energy < cfg["energy"]:
                m.energy = min(cfg["energy"], m.energy + cfg["energy_regen"] * dt)
        if m.hp < m.max_hp and self.t - m.last_damage > cfg["regen_delay"]:
            m.hp = min(m.max_hp, m.hp + cfg["regen_per_s"] * dt)
        m.gun_cd = max(0.0, m.gun_cd - dt)
        m.laser_cd = max(0.0, m.laser_cd - dt)
        if m.reload > 0:
            m.reload -= dt
            if m.reload <= 0:
                m.ammo = CFG["weapons"]["gun"]["mag"]
                m.reload = 0.0

    # ------------------------------------------------------------- vision
    def can_see(self, viewer, target):
        a = self.a
        vx, vy, vz = viewer.pos
        tx, ty, tz = target.pos
        eye = (vx, vy, vz + CFG["mech"]["eye_height"])
        centre = (tx, ty, tz + 4.5)
        top = (tx, ty, tz + 9.0)
        d = math.hypot(tx - vx, ty - vy)
        if d <= CFG["vision"]["near_range"]:
            return a.visible(eye, centre, top)
        if d <= CFG["vision"]["range"]:
            bearing = math.atan2(ty - vy, tx - vx)
            if abs(ang_diff(viewer.aim[0], bearing)) <= math.radians(CFG["vision"]["fov_deg"]) * 0.5:
                if a.visible(eye, centre, top) and not a.tree_blocked(eye, centre):
                    return True
        return False

    def los(self, a_mech, b_mech):
        a = self.a
        eye = (a_mech.pos[0], a_mech.pos[1], a_mech.pos[2] + CFG["mech"]["eye_height"])
        c = (b_mech.pos[0], b_mech.pos[1], b_mech.pos[2] + 4.5)
        t = (b_mech.pos[0], b_mech.pos[1], b_mech.pos[2] + 9.0)
        return a.visible(eye, c, t)

    def shot_los(self, a_mech, b_mech):
        eye = (a_mech.pos[0], a_mech.pos[1], a_mech.pos[2] + CFG["mech"]["muzzle_height"])
        c = (b_mech.pos[0], b_mech.pos[1], b_mech.pos[2] + 4.5)
        return not self.a.segment_blocked(eye, c, terrain=False)

    # ------------------------------------------------------------- weapons
    def fire_gun(self, m, target_pos):
        muzzle = (m.pos[0], m.pos[1], m.pos[2] + CFG["mech"]["muzzle_height"])
        d = math.hypot(target_pos[0] - muzzle[0], target_pos[1] - muzzle[1])
        if d < 1e-3:
            return
        yaw = math.atan2(target_pos[1] - muzzle[1], target_pos[0] - muzzle[0])
        dx, dy = math.cos(yaw), math.sin(yaw)
        ox = muzzle[0] + dx * 3.0
        oy = muzzle[1] + dy * 3.0
        vx = dx * CFG["weapons"]["gun"]["speed"]
        vy = dy * CFG["weapons"]["gun"]["speed"]
        self.bullets.append({"owner": m.id, "pos": [ox, oy, muzzle[2]],
                             "vel": [vx, vy, 0.0], "life": 1.2})
        m.ammo -= 1
        if m.ammo <= 0:
            m.reload = CFG["weapons"]["gun"]["reload"]
        m.gun_cd = CFG["weapons"]["gun"]["cooldown"]

    def damage(self, victim, amount, weapon, by, bearing=None):
        if not victim.alive:
            return
        amount *= (1.0 - victim.armor)
        victim.hp -= amount
        victim.last_damage = self.t
        if victim is self.player:
            self.player_damage += amount
            self.player_damage_by[by] = self.player_damage_by.get(by, 0.0) + amount
            rear = bearing is not None and abs(ang_diff(bearing, victim.aim[0])) > math.pi / 2
            if bearing is None:
                bearing = math.atan2(0, 1)
            self.events.append({"type": "damaged", "amount": round(amount, 1),
                                "weapon": weapon, "bearing": round(bearing, 3),
                                "by": by, "rear": bool(rear)})
        if victim.hp <= 0:
            victim.hp = 0.0
            victim.alive = False
            self.events.append({"type": "death", "id": victim.id})
            if victim is self.player:
                pass

    # ------------------------------------------------------------- hunters
    def hunter_senses(self, h, target):
        """House-bot vision: same range/FOV/canopy rules as the player."""
        a = self.a
        vx, vy, vz = h.pos
        tx, ty, tz = target.pos
        eye = (vx, vy, vz + CFG["mech"]["eye_height"])
        centre = (tx, ty, tz + 4.5)
        top = (tx, ty, tz + 9.0)
        d = math.hypot(tx - vx, ty - vy)
        if d <= CFG["vision"]["near_range"]:
            return a.visible(eye, centre, top)
        if d <= CFG["vision"]["range"]:
            bearing = math.atan2(ty - vy, tx - vx)
            if abs(ang_diff(h.aim[0], bearing)) <= math.radians(CFG["vision"]["fov_deg"]) * 0.5:
                if a.visible(eye, centre, top) and not a.tree_blocked(eye, centre):
                    return True
        return False

    def hunter_act(self, h, dt):
        p = self.player
        px, py = p.pos[0], p.pos[1]
        hx, hy = h.pos[0], h.pos[1]
        d = math.hypot(px - hx, py - hy)
        los = self.hunter_senses(h, p)
        tier = h.tier
        if p.alive and los:
            self.mem[h.id] = (self.t, p.pos[0], p.pos[1])
        last = self.mem.get(h.id)
        tx, ty = (last[1], last[2]) if last else (px, py)

        # preferred range
        if tier == "sniper":
            want = 145.0
        elif tier == "expert":
            want = 120.0
        elif tier == "rusher":
            want = 25.0
        else:
            want = 45.0
        mx = my = 0.0
        if not p.alive:
            mx, my = unit(self.zone.center[0] - hx, self.zone.center[1] - hy)
        elif d > want + 18:
            mx, my = unit(tx - hx, ty - hy)
        elif d < want - 22:
            mx, my = unit(hx - tx, hy - ty)
        else:
            # strafe around the target
            ux, uy = unit(tx - hx, ty - hy)
            mx, my = -uy * h.strafe, ux * h.strafe
            h.strafe_t = getattr(h, "strafe_t", 0.0) + dt
            if h.strafe_t > self.rng.uniform(1.5, 3.5):
                h.strafe *= -1
                h.strafe_t = 0.0
        # stay in the zone
        zc = self.zone.center
        zr = self.zone.radius
        zd = math.hypot(hx - zc[0], hy - zc[1])
        if zd > zr - 15:
            ux, uy = unit(zc[0] - hx, zc[1] - hy)
            mx, my = ux, uy
        # aim
        want_yaw = math.atan2(ty - hy, tx - hx)
        h.aim[0] += max(-CFG["mech"]["turn_rate"] * dt,
                        min(CFG["mech"]["turn_rate"] * dt, ang_diff(want_yaw, h.aim[0])))
        aimerr = abs(ang_diff(h.aim[0], math.atan2(py - hy, px - hx)))

        # fire
        if p.alive and los:
            if "laser" in h.weapons:
                if not h.charging:
                    if h.laser_cd <= 0 and aimerr < 0.04 and h.energy > 10:
                        h.charging = True
                        h.charge = 0.0
                else:
                    h.charge += dt
                    h.energy = max(0.0, h.energy - 2.0 * dt)
                    if aimerr > 0.12 or not los:
                        h.charging = False
                        h.charge = 0.0
                    elif h.charge >= CFG["weapons"]["laser"]["charge"]:
                        self.damage(p, CFG["weapons"]["laser"]["damage"], "laser", h.id,
                                    math.atan2(hy - py, hx - px))
                        h.charging = False
                        h.charge = 0.0
                        h.laser_cd = CFG["weapons"]["laser"]["cooldown"]
            if h.gun_cd <= 0 and h.ammo > 0 and aimerr < 0.05:
                # lead the player a little
                sp = CFG["weapons"]["gun"]["speed"]
                tt = d / sp
                self.fire_gun(h, (px + p.vel[0] * tt, py + p.vel[1] * tt, p.pos[2] + 4.5))
        else:
            h.charging = False
        self.step_mech(h, [mx, my], False, False, dt)

    # --------------------------------------------------------------- player
    def make_obs(self, tick, event_list):
        p = self.player
        a = self.a
        vis = []
        now = self.t
        # visible hunters
        for h in self.hunters:
            if not h.alive:
                continue
            if self.can_see(p, h):
                self.pmem[h.id] = (now, h.pos[0], h.pos[1])
                vis.append({"id": h.id, "kind": "mech",
                            "pos": [round(v, 3) for v in h.pos],
                            "vel": [round(v, 3) for v in h.vel],
                            "aim": [round(h.aim[0], 3), 0.0],
                            "hp": round(h.hp, 1), "max_hp": h.max_hp,
                            "cubes": h.cubes, "weapons": list(h.weapons),
                            "charging": h.charging})
        # memory
        seen = {m["id"] for m in vis}
        for hid, (mt, mx, my) in list(self.pmem.items()):
            if now - mt <= CFG["vision"]["memory_s"] and hid not in seen:
                h = next((q for q in self.hunters if q.id == hid), None)
                if h and h.alive:
                    vis.append({"id": hid, "kind": "mech",
                                "pos": [round(mx, 3), round(my, 3), round(h.pos[2], 3)],
                                "vel": [0, 0, 0], "aim": [0, 0],
                                "hp": None, "max_hp": h.max_hp,
                                "cubes": 0, "weapons": list(h.weapons),
                                "charging": h.charging})
        # projectiles we can see (enemy bullets)
        projs = []
        for b in self.bullets:
            if b["owner"] == 0:
                continue
            bp = b["pos"]
            eye = (p.pos[0], p.pos[1], p.pos[2] + 8)
            if math.hypot(bp[0] - p.pos[0], bp[1] - p.pos[1]) < 45:
                projs.append({"id": id(b) % 100000, "kind": "gun",
                              "pos": [round(v, 2) for v in bp],
                              "vel": [round(v, 2) for v in b["vel"]]})
            else:
                if (abs(ang_diff(p.aim[0], math.atan2(bp[1] - p.pos[1], bp[0] - p.pos[0]))
                        <= math.radians(60))
                        and not a.segment_blocked(eye, bp, terrain=False)):
                    projs.append({"id": id(b) % 100000, "kind": "gun",
                                  "pos": [round(v, 2) for v in bp],
                                  "vel": [round(v, 2) for v in b["vel"]]})
        # sounds
        sounds = []
        for s in self.sounds:
            owner, kind, pos = s
            if owner == 0:
                continue
            carry = CFG["sound"]["range"].get(kind, 0)
            d = math.hypot(pos[0] - p.pos[0], pos[1] - p.pos[1])
            if d > carry:
                continue
            b = math.atan2(pos[1] - p.pos[1], pos[0] - p.pos[0])
            b += self.rng.gauss(0, math.radians(CFG["sound"]["bearing_noise_deg"]))
            dclass = "near" if d < CFG["sound"]["near"] else ("mid" if d < CFG["sound"]["mid"] else "far")
            sounds.append({"kind": kind, "bearing": round(b, 3),
                           "dist": dclass, "age": round(now - s[3], 2) if len(s) > 3 else 0.0})
        self.sounds = []
        # cubes
        cubes = []
        for c in self.cubes:
            if math.hypot(c[0] - p.pos[0], c[1] - p.pos[1]) < 20:
                cubes.append({"id": c[2], "pos": [c[0], c[1], c[3]]})
        z = self.zone
        zp = p.pos[2]
        in_zone = math.hypot(p.pos[0] - z.center[0], p.pos[1] - z.center[1]) <= z.radius
        return {
            "type": "obs", "tick": tick, "t": round(now, 2), "alive": 15,
            "self": {
                "id": 0, "pos": [round(v, 3) for v in p.pos],
                "vel": [round(v, 3) for v in p.vel],
                "aim": [round(p.aim[0], 4), round(p.aim[1], 4)],
                "hp": round(p.hp, 1), "max_hp": p.max_hp,
                "energy": round(p.energy, 1), "max_energy": 100,
                "grounded": p.grounded, "cubes": p.cubes, "upgrades": [],
                "weapons": [{"kind": "gun", "cooldown": round(p.gun_cd, 3),
                             "ammo": p.ammo, "reload": round(p.reload, 3),
                             "stats": dict(CFG["weapons"]["gun"])}, None],
                "in_zone": in_zone, "stats": dict(CFG["mech"]),
            },
            "mechs": vis, "projectiles": projs, "crates": [], "cubes": cubes,
            "pickups": [], "sounds": sounds, "beams": [],
            "events": event_list,
            "zone": {"center": [round(z.center[0], 2), round(z.center[1], 2)],
                     "radius": round(z.radius, 1),
                     "next_center": [round(z.next_center[0], 2), round(z.next_center[1], 2)],
                     "next_radius": round(z.next_radius, 1),
                     "shrink_start": round(z.shrink_start, 2),
                     "shrink_end": round(z.shrink_end, 2), "dps": z.dps},
            "offer": None, "drops": [],
        }

    # ----------------------------------------------------------------- run
    def run(self, bot, max_t=480.0, verbose=False):
        dt = 0.1
        tick = 0
        self.sounds = []
        # a handful of cubes so looting is exercisable
        for i in range(14):
            sp = self._random_spawn()
            self.cubes.append((sp[0], sp[1], 5000 + i, sp[2]))
        bot_aim = [0.0, 0.0]
        while self.t < max_t and self.player.alive:
            self.zone.at(self.t)
            evs = self.events
            self.events = []
            obs = self.make_obs(tick, evs)
            act = bot.act(obs)
            mv = act.get("move", [0, 0])
            boost = bool(act.get("boost", False))
            jump = bool(act.get("jump", False))
            if "aim" in act:
                bot_aim = act["aim"]
                # slew
                tr = CFG["mech"]["turn_rate"] * dt
                self.player.aim[0] += max(-tr, min(tr, ang_diff(bot_aim[0], self.player.aim[0])))
                self.player.aim[1] += max(-3 * dt, min(3 * dt, bot_aim[1] - self.player.aim[1]))
            fire = act.get("fire", [False, False])
            self.step_mech(self.player, mv, boost, jump, dt)
            if fire and fire[0] and self.player.gun_cd <= 0 and self.player.ammo > 0:
                # shoot along the actual aim
                yaw = self.player.aim[0]
                muzzle = (self.player.pos[0] + math.cos(yaw) * 3.0,
                          self.player.pos[1] + math.sin(yaw) * 3.0,
                          self.player.pos[2] + CFG["mech"]["muzzle_height"])
                # aim ray vs hunters
                best = None
                bd = 1e9
                for h in self.hunters:
                    if not h.alive:
                        continue
                    d = math.hypot(h.pos[0] - self.player.pos[0], h.pos[1] - self.player.pos[1])
                    if d > CFG["weapons"]["gun"]["range"]:
                        continue
                    b = math.atan2(h.pos[1] - muzzle[1], h.pos[0] - muzzle[0])
                    if abs(ang_diff(b, yaw)) < 0.05 and d < bd:
                        bd = d
                        best = h
                vx = math.cos(yaw) * CFG["weapons"]["gun"]["speed"]
                vy = math.sin(yaw) * CFG["weapons"]["gun"]["speed"]
                self.bullets.append({"owner": 0, "pos": list(muzzle), "vel": [vx, vy, 0.0],
                                     "life": 1.2, "aimed": best.id if best else None})
                self.player.ammo -= 1
                self.player.gun_cd = CFG["weapons"]["gun"]["cooldown"]
                self.sounds.append((0, "gun", self.player.pos))
            for s in evs:
                if s.get("type") == "damaged":
                    self.player_damage += 0.0

            # hunters
            for h in self.hunters:
                if not h.alive:
                    continue
                if h.charging:
                    self.sounds.append((h.id, "laser_charge", h.pos))
                if h.gun_cd > 0.23:
                    self.sounds.append((h.id, "gun", h.pos))
                self.hunter_act(h, dt)
                if math.hypot(h.vel[0], h.vel[1]) > 5:
                    self.sounds.append((h.id, "step", h.pos))
                # zone damage
                if math.hypot(h.pos[0] - self.zone.center[0],
                              h.pos[1] - self.zone.center[1]) > self.zone.radius:
                    h.hp -= self.zone.dps * dt
                    h.last_damage = self.t
                    if h.hp <= 0:
                        h.alive = False
                        self.events.append({"type": "death", "id": h.id})

            # player zone damage
            if math.hypot(self.player.pos[0] - self.zone.center[0],
                          self.player.pos[1] - self.zone.center[1]) > self.zone.radius:
                self.zone_damage += self.zone.dps * dt
                self.damage(self.player, self.zone.dps * dt, "zone", None, None)

            # bullets
            for b in list(self.bullets):
                b["pos"][0] += b["vel"][0] * dt
                b["pos"][1] += b["vel"][1] * dt
                b["life"] -= dt
                hit = False
                if b["owner"] != 0:
                    p = self.player
                    if (math.hypot(b["pos"][0] - p.pos[0], b["pos"][1] - p.pos[1]) < 2.5
                            and p.pos[2] - 1 <= b["pos"][2] <= p.pos[2] + 9):
                        self.damage(p, CFG["weapons"]["gun"]["damage"], "gun", b["owner"],
                                    math.atan2(-b["vel"][1], -b["vel"][0]))
                        hit = True
                else:
                    for h in self.hunters:
                        if not h.alive:
                            continue
                        if (math.hypot(b["pos"][0] - h.pos[0], b["pos"][1] - h.pos[1]) < 2.5
                                and h.pos[2] - 1 <= b["pos"][2] <= h.pos[2] + 9):
                            self.damage(h, CFG["weapons"]["gun"]["damage"], "gun", 0,
                                        math.atan2(h.pos[1] - self.player.pos[1],
                                                   h.pos[0] - self.player.pos[0]))
                            hit = True
                            break
                if hit or b["life"] <= 0:
                    self.bullets.remove(b)

            # cube pickup
            for c in list(self.cubes):
                if (math.hypot(c[0] - self.player.pos[0], c[1] - self.player.pos[1]) < 3.5
                        and abs(c[3] - self.player.pos[2]) < 4):
                    self.cubes.remove(c)
                    self.player.cubes += 1
                    self.player.max_hp *= 1.04
                    self.player.hp = min(self.player.max_hp, self.player.hp + self.player.max_hp * 0.04)

            self.t += dt
            tick += 2
            if verbose and int(tick) % 20 == 0:
                d = min((math.hypot(h.pos[0] - self.player.pos[0], h.pos[1] - self.player.pos[1])
                         for h in self.hunters if h.alive), default=999)
                print(f"t{self.t:5.1f} hp{self.player.hp:6.0f} pos({self.player.pos[0]:6.0f},"
                      f"{self.player.pos[1]:6.0f}) d{d:5.0f} mode {getattr(bot, 'mode', '?')}")
        self.time_alive = self.t
        return {"alive": self.player.alive, "time": self.t, "damage": self.player_damage,
                "zone_damage": self.zone_damage, "hp": self.player.hp,
                "by": self.player_damage_by}


def main():
    import ghost
    a = Arena("mechfight-kit/arena")
    n = int(sys.argv[sys.argv.index("-n") + 1]) if "-n" in sys.argv else 20
    seeds = range(n)
    verbose = int(sys.argv[sys.argv.index("-v") + 1]) if "-v" in sys.argv else None
    tiers = None
    if "-t" in sys.argv:
        tiers = sys.argv[sys.argv.index("-t") + 1].split(",")
    tot = 0.0
    times = []
    for s in seeds:
        m = Match(a, seed=1000 + s, n_hunters=len(tiers) if tiers else 3, tiers=tiers)
        bot = ghost.Bot({"config": CFG, "seed": s, "slot": 0,
                         "map": {"dir": "mechfight-kit/arena"}, "_arena": a})
        if verbose == s:
            print("=== seed", s)
        r = m.run(bot, verbose=(verbose == s))
        times.append(r["time"])
        tot += r["damage"]
        print(f"seed {s:3d} alive={r['alive']} t={r['time']:5.1f} "
              f"dmg={r['damage']:6.0f} zone={r['zone_damage']:5.0f} cubes={bot.cubes}")
    print("mean damage", round(tot / len(seeds)), "median survival", sorted(times)[len(times) // 2])


if __name__ == "__main__":
    main()
