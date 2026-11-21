"""Stealth/camouflage bot: avoids fights, farms safely, survives."""
import math
import os
import random
import sys
import struct
import json
import array
import mechfight as mf


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def dist3(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)


def ang_diff(a, b):
    d = (a - b + math.pi) % (2 * math.pi) - math.pi
    return d


class MapData:
    def __init__(self, map_dir):
        self.map_dir = map_dir
        world_path = os.path.join(map_dir, "v2", "world.json")
        if not os.path.exists(world_path):
            # search
            for root, _, files in os.walk(map_dir):
                if "world.json" in files:
                    world_path = os.path.join(root, "world.json")
                    break
        with open(world_path) as f:
            self.world = json.load(f)
        t = self.world["terrain"]
        self.nx = t["nx"]
        self.ny = t["ny"]
        self.step = t["step"]
        self.minX = t["minX"]
        self.minY = t["minY"]
        terrain_path = os.path.join(os.path.dirname(world_path), "terrain.i16")
        if not os.path.exists(terrain_path):
            for root, _, files in os.walk(map_dir):
                if "terrain.i16" in files:
                    terrain_path = os.path.join(root, "terrain.i16")
                    break
        with open(terrain_path, "rb") as f:
            raw = f.read()
        arr = array.array("h")
        arr.frombytes(raw)
        # data is little-endian; swap if running on big-endian
        if sys.byteorder != "little":
            arr.byteswap()
        self.terrain = arr

        self.prisms = []
        for p in self.world.get("prisms", []):
            ring = p["ring"]
            poly = [(ring[k], ring[k+1]) for k in range(0, len(ring), 2)]
            holes = []
            for h in p.get("holes", []):
                holes.append([(h[k], h[k+1]) for k in range(0, len(h), 2)])
            self.prisms.append({
                "kind": p.get("kind"),
                "base": p["base"],
                "top": p["top"],
                "poly": poly,
                "holes": holes,
            })

        tf = self.world["trees"]["fields"]
        data = self.world["trees"]["data"]
        nfields = len(tf)
        idx = {name: i for i, name in enumerate(tf)}
        self.trees = []
        for i in range(0, len(data), nfields):
            row = data[i:i+nfields]
            self.trees.append({
                "x": row[idx["x"]],
                "y": row[idx["y"]],
                "z": row[idx["z"]],
                "height": row[idx["height"]],
                "crownR": row[idx["crownR"]],
                "crownBase": row[idx["crownBase"]],
                "trunkR": row[idx["trunkR"]],
                "crownX": row[idx["crownX"]],
                "crownY": row[idx["crownY"]],
            })

        # spatial grids
        self.cell = 40.0
        self.prisms_by_cell = {}
        for i, p in enumerate(self.prisms):
            xs = [pt[0] for pt in p["poly"]]
            ys = [pt[1] for pt in p["poly"]]
            xmin, xmax = min(xs), max(xs)
            ymin, ymax = min(ys), max(ys)
            p["bbox"] = (xmin, xmax, ymin, ymax)
            for cx in range(int(xmin // self.cell) - 1, int(xmax // self.cell) + 2):
                for cy in range(int(ymin // self.cell) - 1, int(ymax // self.cell) + 2):
                    self.prisms_by_cell.setdefault((cx, cy), []).append(i)
        self.trees_by_cell = {}
        for i, tr in enumerate(self.trees):
            xmin = tr["x"] - tr["crownR"] - 1
            xmax = tr["x"] + tr["crownR"] + 1
            ymin = tr["y"] - tr["crownR"] - 1
            ymax = tr["y"] + tr["crownR"] + 1
            for cx in range(int(xmin // self.cell) - 1, int(xmax // self.cell) + 2):
                for cy in range(int(ymin // self.cell) - 1, int(ymax // self.cell) + 2):
                    self.trees_by_cell.setdefault((cx, cy), []).append(i)

    def height(self, x, y):
        nx, ny = self.nx, self.ny
        step = self.step
        minX, minY = self.minX, self.minY
        fx = (x - minX) / step
        fy = (y - minY) / step
        i0 = int(math.floor(fx))
        j0 = int(math.floor(fy))
        if i0 < 0:
            i0 = 0
        if i0 > nx - 2:
            i0 = nx - 2
        if j0 < 0:
            j0 = 0
        if j0 > ny - 2:
            j0 = ny - 2
        i1 = i0 + 1
        j1 = j0 + 1
        tx = fx - i0
        ty = fy - j0
        def get(i, j):
            return self.terrain[j * nx + i] * 0.01
        h00 = get(i0, j0)
        h10 = get(i1, j0)
        h01 = get(i0, j1)
        h11 = get(i1, j1)
        return (h00 * (1 - tx) * (1 - ty) + h10 * tx * (1 - ty) +
                h01 * (1 - tx) * ty + h11 * tx * ty)

    @staticmethod
    def _point_in_poly(poly, x, y):
        inside = False
        n = len(poly)
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > y) != (yj > y)):
                xint = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < xint:
                    inside = not inside
            j = i
        return inside

    def _candidate_prisms(self, xmin, xmax, ymin, ymax):
        res = set()
        for cx in range(int(xmin // self.cell) - 1, int(xmax // self.cell) + 2):
            for cy in range(int(ymin // self.cell) - 1, int(ymax // self.cell) + 2):
                for i in self.prisms_by_cell.get((cx, cy), []):
                    res.add(i)
        return res

    def point_in_prism(self, x, y, z):
        cx = int(x // self.cell)
        cy = int(y // self.cell)
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.prisms_by_cell.get((cx + dx, cy + dy), []):
                    seen.add(i)
        for i in seen:
            p = self.prisms[i]
            if p["base"] <= z <= p["top"]:
                if self._point_in_poly(p["poly"], x, y):
                    in_hole = False
                    for h in p["holes"]:
                        if self._point_in_poly(h, x, y):
                            in_hole = True
                            break
                    if not in_hole:
                        return True
        return False

    def segment_blocked_prism(self, a, b):
        ax, ay, az = a
        bx, by, bz = b
        zmin = min(az, bz)
        zmax = max(az, bz)
        xmin, xmax = (ax, bx) if ax < bx else (bx, ax)
        ymin, ymax = (ay, by) if ay < by else (by, ay)
        seen = self._candidate_prisms(xmin, xmax, ymin, ymax)
        for i in seen:
            p = self.prisms[i]
            if p["top"] < zmin or p["base"] > zmax:
                continue
            poly = p["poly"]
            if (self._point_in_poly(poly, ax, ay) and not any(self._point_in_poly(h, ax, ay) for h in p["holes"])) or \
               (self._point_in_poly(poly, bx, by) and not any(self._point_in_poly(h, bx, by) for h in p["holes"])):
                if p["base"] <= az <= p["top"] or p["base"] <= bz <= p["top"]:
                    return True
            n = len(poly)
            for j in range(n):
                x1, y1 = poly[j]
                x2, y2 = poly[(j+1) % n]
                if self._seg_intersect(ax, ay, bx, by, x1, y1, x2, y2):
                    return True
        return False

    @staticmethod
    def _seg_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
        d1 = (x3 - x4) * (y1 - y3) + (y3 - y4) * (x3 - x1)
        d2 = (x3 - x4) * (y2 - y3) + (y3 - y4) * (x3 - x2)
        d3 = (x1 - x2) * (y3 - y1) + (y1 - y2) * (x3 - x1)
        d4 = (x1 - x2) * (y4 - y1) + (y1 - y2) * (x4 - x1)
        if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
            return True
        return False

    def tree_blocks_vision(self, a, b):
        ax, ay, az = a
        bx, by, bz = b
        xmin, xmax = (ax, bx) if ax < bx else (bx, ax)
        ymin, ymax = (ay, by) if ay < by else (by, ay)
        seen = set()
        for cx in range(int(xmin // self.cell) - 1, int(xmax // self.cell) + 2):
            for cy in range(int(ymin // self.cell) - 1, int(ymax // self.cell) + 2):
                for i in self.trees_by_cell.get((cx, cy), []):
                    seen.add(i)
        for i in seen:
            tr = self.trees[i]
            # check distance from segment to trunk center in 2D
            cx, cy = tr["x"], tr["y"]
            r = tr["trunkR"]
            # project point on segment
            dx = bx - ax
            dy = by - ay
            if dx == 0 and dy == 0:
                continue
            t = ((cx - ax) * dx + (cy - ay) * dy) / (dx * dx + dy * dy)
            if t < 0:
                t = 0
            if t > 1:
                t = 1
            px = ax + dx * t
            py = ay + dy * t
            if math.hypot(px - cx, py - cy) <= r + 0.3:
                # z overlap with trunk
                zt = az + (bz - az) * t
                trunk_top = tr["z"] + tr["crownBase"]
                if tr["z"] <= zt <= trunk_top or tr["z"] <= az <= trunk_top or tr["z"] <= bz <= trunk_top:
                    return True
            # crown sphere
            cr = tr["crownR"]
            ccx, ccy = tr["crownX"], tr["crownY"]
            cz = tr["z"] + tr["crownBase"] + cr * 0.5
            # distance from segment to crown center
            dx = bx - ax
            dy = by - ay
            dz = bz - az
            # param t
            # vector from a to center
            vx = ccx - ax
            vy = ccy - ay
            vz = cz - az
            denom = dx*dx + dy*dy + dz*dz
            if denom == 0:
                continue
            t = (vx*dx + vy*dy + vz*dz) / denom
            if t < 0:
                t = 0
            if t > 1:
                t = 1
            closest_x = ax + dx*t
            closest_y = ay + dy*t
            closest_z = az + dz*t
            d = math.sqrt((closest_x-ccx)**2 + (closest_y-ccy)**2 + (closest_z-cz)**2)
            if d <= cr:
                return True
        return False

    def has_los(self, a, b):
        if self.segment_blocked_prism(a, b):
            return False
        if self.tree_blocks_vision(a, b):
            return False
        return True


class Bot:
    name = "Ghost"

    def __init__(self, hello):
        self.hello = hello
        self.cfg = hello["config"]
        self.slot = hello["slot"]
        self.seed = hello["seed"]
        self.rng = random.Random(int(self.seed))
        self.map = MapData(hello["map"]["dir"])
        self.last_move = [0.0, 0.0]
        self.last_aim = [0.0, 0.0]
        self.last_boost = False
        self.last_damaged_t = -999.0
        self.safe_angle = self.rng.uniform(0, 2 * math.pi)
        self.flee_until = 0.0
        self.mode = "position"
        self.eye_height = self.cfg["mech"].get("eye_height", 8.0)
        # mech stats defaults
        self.walk_speed = self.cfg["mech"].get("walk_speed", 11.7)

    def act(self, obs):
        mf.draw.clear()
        me = obs["self"]
        pos = me["pos"]
        x, y, z = pos
        t = obs["t"]
        cfg = self.cfg
        mech = me["stats"]
        walk_speed = mech.get("walk_speed", self.walk_speed)
        eye_h = mech.get("eye_height", self.eye_height)
        muzzle_h = mech.get("muzzle_height", 7.0)

        # track damage
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damaged_t = t

        enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]

        # threat list
        threats = []
        for e in enemies:
            d = dist3(e["pos"], pos)
            # enemy eye
            e_eye = (e["pos"][0], e["pos"][1], e["pos"][2] + 8.0)
            our_center = (x, y, z + 4.5)
            our_top = (x, y, z + 9.0)
            los_to_us = self.map.has_los(e_eye, our_center) or self.map.has_los(e_eye, our_top)
            dir_to_us = math.atan2(pos[1] - e["pos"][1], pos[0] - e["pos"][0])
            aim_err = abs(ang_diff(e["aim"][0], dir_to_us))
            aim_at_us = aim_err < 0.35
            threats.append({
                "id": e["id"],
                "pos": e["pos"],
                "dist": d,
                "los_to_us": los_to_us,
                "aim_at_us": aim_at_us,
                "hp": e.get("hp", 1400),
                "vel": e.get("vel", [0, 0, 0]),
            })

        # pseudo-threats from nearby weapon sounds
        danger_sound_near = False
        for s in obs.get("sounds", []):
            kind = s.get("kind", "")
            if kind in ("gun", "mortar", "explosion", "laser", "lightning", "npc_gun", "crate"):
                if s.get("dist") == "near" and s.get("age", 99) < 1.5:
                    danger_sound_near = True
                    bearing = s.get("bearing", 0.0)
                    est = 45.0
                    px = x + math.cos(bearing) * est
                    py = y + math.sin(bearing) * est
                    threats.append({"id": -999, "pos": [px, py, z], "dist": est,
                                    "los_to_us": False, "aim_at_us": False, "hp": 9999,
                                    "vel": [0, 0, 0]})
        nearest_threat_dist = min((th["dist"] for th in threats), default=9999.0)
        spotted = any(th["los_to_us"] and th["dist"] < cfg["vision"]["range"] and th["dist"] < 200 for th in threats)
        shot_recently = (t - self.last_damaged_t) < 2.0

        # zone
        zone = obs["zone"]
        zc = zone["center"]
        zr = zone["radius"]
        nzc = zone["next_center"]
        nzr = zone["next_radius"]
        in_zone = me["in_zone"]
        zone_margin = zr - math.hypot(x - zc[0], y - zc[1])
        force_zone = zone_margin < 22

        move = [0.0, 0.0]
        boost = False
        jump = False
        aim = list(self.last_aim) if self.last_aim else [0.0, 0.0]
        fire = [False, False]
        combat_target = None

        # --- flee conditions ---
        # supply drop capture (computed before flee)
        capturing_drop = None
        for drop in obs.get("drops", []):
            if drop.get("lands_in", 1) > 0:
                continue
            dd = dist3(drop["pos"], pos)
            if dd > 220:
                continue
            if any(dist3(e["pos"], drop["pos"]) < 70 for e in enemies):
                continue
            capturing_drop = drop
            break

        flee = False
        if nearest_threat_dist < 30:
            flee = True
        if shot_recently:
            flee = True
        if spotted and nearest_threat_dist < 90:
            flee = True
        if me["hp"] < me["max_hp"] * 0.35 and nearest_threat_dist < 120:
            flee = True
        if not in_zone:
            flee = True
        if capturing_drop is not None and nearest_threat_dist < 70:
            flee = True
        if danger_sound_near and nearest_threat_dist < 60:
            flee = True

        real_threats = [th for th in threats if th.get("id", 0) >= 0]
        # flee if outnumbered
        if sum(1 for th in real_threats if th["dist"] < 70) >= 2:
            flee = True

        # combat candidate
        combat_target = None
        if not flee and real_threats:
            nt = min(real_threats, key=lambda t: t["dist"])
            if nt["dist"] < 120 and me["hp"] > me["max_hp"] * 0.35:
                engage = False
                if nt["dist"] < 60:
                    engage = True
                elif spotted and nt["dist"] < 120:
                    engage = True
                elif len([t for t in real_threats if t["dist"] < 90]) == 1 and nt["dist"] < 110:
                    engage = True
                if engage:
                    combat_target = nt

        # farming target
        farm_goal = None
        if not flee and not combat_target and capturing_drop is None and not danger_sound_near and not force_zone:
            # cubes (silent, lower safety threshold)
            if obs.get("cubes") and nearest_threat_dist > 60:
                c = min(obs["cubes"], key=lambda c: dist2(c["pos"], pos))
                if dist2(c["pos"], pos) < 150:
                    farm_goal = c["pos"]
            # crates
            if farm_goal is None and obs.get("crates") and nearest_threat_dist > 80:
                cr = min(obs["crates"], key=lambda c: dist2(c["pos"], pos))
                if dist2(cr["pos"], pos) < 120:
                    farm_goal = cr["pos"]
            # npcs (noisy)
            if farm_goal is None and npcs and nearest_threat_dist > 90:
                n = min(npcs, key=lambda n: dist3(n["pos"], pos))
                if dist3(n["pos"], pos) < 160:
                    farm_goal = n["pos"]
            # weapon pickups (empty slot)
            held_kinds = [w["kind"] if w else None for w in me["weapons"]]
            if farm_goal is None and me["weapons"][1] is None:
                near_p = [p for p in obs.get("pickups", []) if dist2(p["pos"], pos) < cfg["loot"]["pickup_radius"] + 3]
                if near_p:
                    # prefer non-gun
                    p = min(near_p, key=lambda p: (0 if p["kind"] != "gun" else 1, dist2(p["pos"], pos)))
                    farm_goal = p["pos"]

        # positioning goal
        if flee:
            # repulsion
            rx, ry = 0.0, 0.0
            for th in threats:
                if th["dist"] < 120:
                    w = 1.0 / (th["dist"] + 5.0)
                    rx += (x - th["pos"][0]) * w
                    ry += (y - th["pos"][1]) * w
            if not in_zone:
                rx += (zc[0] - x) * 0.6
                ry += (zc[1] - y) * 0.6
            nrm = math.hypot(rx, ry)
            if nrm > 0:
                move = [rx / nrm, ry / nrm]
            else:
                move = [0.0, 0.0]
            boost = (nearest_threat_dist < 70) or (not in_zone)
            # jump if needed to climb
            # aim at nearest threat to keep vision
            if threats:
                nt = min(threats, key=lambda t: t["dist"])
                aim[0] = math.atan2(nt["pos"][1] - y, nt["pos"][0] - x)
                aim[1] = 0.0
                if nt["dist"] < 40 and not nt.get("sound"):
                    gun = me["weapons"][0]
                    if gun is not None and gun.get("cooldown", 0) <= 0.05:
                        fire[0] = True
            self.mode = "flee"
        elif combat_target is not None:
            ct = combat_target
            # aim with lead
            d = ct["dist"]
            gs = cfg["weapons"]["gun"]["speed"]
            tlead = d / max(gs, 1.0)
            lx = ct["pos"][0] + ct["vel"][0] * tlead
            ly = ct["pos"][1] + ct["vel"][1] * tlead
            lz = ct["pos"][2] + ct["vel"][2] * tlead
            aim[0] = math.atan2(ly - y, lx - x)
            aim[1] = math.atan2(lz + 4.5 - (z + muzzle_h), math.hypot(lx - x, ly - y))
            # kite away
            kx = x - ct["pos"][0]
            ky = y - ct["pos"][1]
            kn = math.hypot(kx, ky)
            if kn > 0:
                move = [kx / kn * 0.85, ky / kn * 0.85]
            boost = d < 25
            # fire gun
            gun = me["weapons"][0]
            if gun is not None and gun.get("cooldown", 0) <= 0.05:
                fire[0] = True
            # lightning if slot1 and close
            w1 = me["weapons"][1]
            if w1 is not None and w1["kind"] == "lightning" and d < 42 and me["energy"] > 6:
                if w1.get("cooldown", 0) <= 0.05:
                    fire[1] = True
            self.mode = "combat"
        elif capturing_drop is not None:
            drop = capturing_drop
            dx = drop["pos"][0] - x
            dy = drop["pos"][1] - y
            dn = math.hypot(dx, dy)
            if dn > 0:
                move = [dx / dn, dy / dn]
            boost = False
            # silent if enemies within 120
            if nearest_threat_dist < 120:
                move = [v * 0.5 for v in move]
            else:
                move = [v * 0.9 for v in move]
            self.mode = "drop"
            aim[0] = math.atan2(dy, dx) if dn > 0 else aim[0]
            aim[1] = 0.0
        elif farm_goal is not None:
            gx = farm_goal[0] - x
            gy = farm_goal[1] - y
            gn = math.hypot(gx, gy)
            if gn > 0:
                move = [gx / gn, gy / gn]
            # speed: silent if enemies near else normal
            if nearest_threat_dist < 120:
                move = [v * 0.45 for v in move]
            else:
                move = [v * 0.95 for v in move]
            self.mode = "farm"
            aim[0] = math.atan2(gy, gx) if gn > 0 else aim[0]
            aim[1] = 0.0
            # if close to npc/crate, shoot
            # determine if target is an npc or crate near
            # fire gun if a mech/npc/crate within 60
            shoot_target = None
            # npcs
            for n in npcs:
                if dist3(n["pos"], pos) < 70:
                    shoot_target = n
                    break
            if shoot_target is None:
                for c in obs.get("crates", []):
                    if dist2(c["pos"], pos) < 70:
                        shoot_target = c
                        break
            if shoot_target is not None:
                sp = shoot_target["pos"]
                aim[0] = math.atan2(sp[1] - y, sp[0] - x)
                aim[1] = math.atan2(sp[2] + 2 - (z + muzzle_h), math.hypot(sp[0]-x, sp[1]-y))
                gun = me["weapons"][0]
                if gun is not None and gun.get("cooldown", 0) <= 0.05:
                    fire[0] = True
        elif force_zone:
            dx = nzc[0] - x
            dy = nzc[1] - y
            dn = math.hypot(dx, dy)
            if dn > 0:
                move = [dx / dn, dy / dn]
            else:
                move = [0.0, 0.0]
            boost = zone_margin < 8
            self.mode = "zone"
            aim[0] = math.atan2(dy, dx) if dn > 0 else aim[0]
            aim[1] = 0.0
        else:
            # positioning toward stealth spot
            # desired angle adjusted away from enemies
            angle = self.safe_angle
            # if enemies cluster, rotate away
            if threats:
                # mean enemy angle from zone center? rotate preferred angle by +pi/2 if enemy near preferred
                # simple: if any enemy within 150 of preferred point, rotate
                pass
            # aim for next zone center (safe), with small personal offset
            gr = min(max(nzr * 0.35, 40.0), 180.0)
            gx = nzc[0] + gr * math.cos(self.safe_angle)
            gy = nzc[1] + gr * math.sin(self.safe_angle)
            # ensure inside current zone
            dgc = math.hypot(gx - zc[0], gy - zc[1])
            if dgc > zr - 20:
                scale = (zr - 20) / max(dgc, 1e-6)
                gx = zc[0] + (gx - zc[0]) * scale
                gy = zc[1] + (gy - zc[1]) * scale
            if any(dist2([gx, gy], [e["pos"][0], e["pos"][1]]) < 60 for e in enemies):
                self.safe_angle += 1.0
                gx = nzc[0] + gr * math.cos(self.safe_angle)
                gy = nzc[1] + gr * math.sin(self.safe_angle)
            dx = gx - x
            dy = gy - y
            dn = math.hypot(dx, dy)
            if dn > 0:
                move = [dx / dn, dy / dn]
            # silent if enemies near
            if nearest_threat_dist < 120:
                move = [v * 0.5 for v in move]
            else:
                move = [v * 0.9 for v in move]
            # toward next zone if current zone shrinking? nudge inward
            if zone.get("shrink_start", 0) <= obs["t"] <= zone.get("shrink_end", 0):
                # move toward next center more
                dx2 = nzc[0] - x
                dy2 = nzc[1] - y
                dn2 = math.hypot(dx2, dy2)
                if dn2 > 0:
                    move = [move[0]*0.5 + (dx2/dn2)*0.5, move[1]*0.5 + (dy2/dn2)*0.5]
            self.mode = "position"

        # terrain avoidance for move
        if move[0] != 0 or move[1] != 0:
            # try to avoid walls
            ang = math.atan2(move[1], move[0])
            best = None
            for off in [0, 0.35, -0.35, 0.7, -0.7, 1.2, -1.2, 2.0, -2.0]:
                a = ang + off
                dx = math.cos(a)
                dy = math.sin(a)
                ok = True
                for s in [2.0, 4.0, 6.0]:
                    nxp = x + dx * s
                    nyp = y + dy * s
                    if self.map.point_in_prism(nxp, nyp, z):
                        ok = False
                        break
                    h = self.map.height(nxp, nyp)
                    if abs(h - z) > 1.3:
                        ok = False
                        break
                if ok:
                    best = (dx, dy)
                    break
            if best is not None:
                move = [best[0] * math.hypot(move[0], move[1]), best[1] * math.hypot(move[0], move[1])]
                # preserve intended speed magnitude
                mag = min(1.0, math.hypot(move[0], move[1]))
                # recompute magnitude from original
            # normalize-ish
            m = math.hypot(move[0], move[1])
            if m > 1.0:
                move = [move[0]/m, move[1]/m]

        # jump for climbing
        if move[0] != 0 or move[1] != 0:
            # sample ahead
            axh = x + move[0] * 3
            ayh = y + move[1] * 3
            hahead = self.map.height(axh, ayh)
            if me["grounded"] and hahead - z > 0.7:
                jump = True

        # weapon pickup handling
        act_extra = {}
        held_kinds = [w["kind"] if w else None for w in me["weapons"]]
        near_pickups = [p for p in obs.get("pickups", []) if dist2(p["pos"], pos) < cfg["loot"]["pickup_radius"] + 1.5]
        if near_pickups:
            # choose best non-held
            best_p = None
            for p in near_pickups:
                if p["kind"] in held_kinds:
                    continue
                if best_p is None or dist2(p["pos"], pos) < dist2(best_p["pos"], pos):
                    best_p = p
            if best_p is not None:
                if me["weapons"][1] is None:
                    act_extra["take"] = best_p["id"]
                else:
                    # swap slot1 if slot1 not gun and not same
                    if me["weapons"][1] is not None and me["weapons"][1]["kind"] != "gun":
                        act_extra["take"] = best_p["id"]
                        act_extra["drop"] = 1

        # upgrades
        if obs.get("offer"):
            choices = obs["offer"]["choices"]
            best_idx = 0
            best_score = -1e9
            for i, c in enumerate(choices):
                cid = c["id"].lower()
                score = 0.0
                if "armor" in cid:
                    score += 3.0
                if "sensor" in cid:
                    score += 2.5
                if "speed" in cid or "turn" in cid or "boost" in cid or "jet" in cid:
                    score += 2.0
                if "hp" in cid or "regen" in cid or "health" in cid:
                    score += 2.0
                if "damage" in cid:
                    score += 1.0
                for wk in held_kinds:
                    if wk and wk in cid:
                        score += 1.5
                if score > best_score:
                    best_score = score
                    best_idx = i
            act_extra["pick"] = best_idx

        # aim smoothing: keep last aim if no target
        if aim is None:
            aim = list(self.last_aim)
        # default aim pitch
        if aim[1] is None:
            aim[1] = 0.0

        # update last continuous
        self.last_move = [float(move[0]), float(move[1])]
        self.last_aim = [float(aim[0]), float(aim[1])]
        self.last_boost = bool(boost)

        # build action
        act = {"tick": obs["tick"], "move": self.last_move, "boost": self.last_boost, "jump": bool(jump),
               "aim": self.last_aim, "fire": fire}
        act.update(act_extra)

        # debug
        mf.draw.label(f"{self.mode} hp{me['hp']:.0f} nthr{nearest_threat_dist:.0f}")
        d = mf.draw.take()
        if d:
            act["debug"] = d
        return act


mf.run(Bot)