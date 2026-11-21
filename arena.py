"""Fast static-map queries for the Mech Royale arena.

World model (protocol.md section 11):
  * terrain: int16 centimetre grid, sampled bilinearly
  * prisms: buildings / stadium / bridges, footprint polygons with holes,
    occupying [base, top]
  * trees: block *vision* only (crowns softly, trunks hard), never shots

Everything is dependency-free.  At construction time the map is baked into
small rasters so that per-decision queries (line of sight, walkability) cost a
few microseconds instead of hundreds -- the match only allows 50 ms/tick.
"""
import array
import json
import math
import os
import sys


def _seg_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
    """Proper (strict) segment intersection via orientation tests."""
    d1 = (x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)
    d2 = (x4 - x3) * (y2 - y3) - (y4 - y3) * (x2 - x3)
    d3 = (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)
    d4 = (x2 - x1) * (y4 - y1) - (y2 - y1) * (x4 - x1)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _point_in_poly(poly, x, y):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xint = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xint:
                inside = not inside
        j = i
    return inside


def _dist_to_poly(poly, x, y):
    best = 1e9
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx = x2 - x1
        dy = y2 - y1
        dd = dx * dx + dy * dy
        if dd <= 1e-9:
            d = math.hypot(x - x1, y - y1)
        else:
            t = ((x - x1) * dx + (y - y1) * dy) / dd
            t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            d = math.hypot(x - (x1 + dx * t), y - (y1 + dy * t))
        if d < best:
            best = d
    return best


class Prism:
    __slots__ = ("kind", "base", "top", "poly", "holes", "bbox", "cx", "cy", "area")

    def __init__(self, kind, base, top, poly, holes):
        self.kind = kind
        self.base = base
        self.top = top
        self.poly = poly
        self.holes = holes
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        self.bbox = (min(xs), max(xs), min(ys), max(ys))
        self.cx = sum(xs) / len(xs)
        self.cy = sum(ys) / len(ys)
        self.area = (max(xs) - min(xs)) * (max(ys) - min(ys))

    def contains(self, x, y, z=None):
        if z is not None and not (self.base <= z <= self.top):
            return False
        if not _point_in_poly(self.poly, x, y):
            return False
        for h in self.holes:
            if _point_in_poly(h, x, y):
                return False
        return True


class Arena:
    G = 2.0          # raster cell size (m)
    TREE_CELL = 16.0

    def __init__(self, map_dir):
        world_path = os.path.join(map_dir, "v2", "world.json")
        if not os.path.exists(world_path):
            for root, _, files in os.walk(map_dir):
                if "world.json" in files:
                    world_path = os.path.join(root, "world.json")
                    break
        with open(world_path) as f:
            world = json.load(f)
        self.world = world

        t = world["terrain"]
        self.nx = t["nx"]
        self.ny = t["ny"]
        self.step = t["step"]
        self.minX = t["minX"]
        self.minY = t["minY"]
        self.maxX = self.minX + (self.nx - 1) * self.step
        self.maxY = self.minY + (self.ny - 1) * self.step
        b = world.get("bounds", {})
        self.bounds = (b.get("minX", self.minX), b.get("maxX", self.maxX),
                       b.get("minY", self.minY), b.get("maxY", self.maxY))

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
        if sys.byteorder != "little":
            arr.byteswap()
        self.terrain = arr
        self.rules = world.get("rules", {})

        self.prisms = []
        for p in world.get("prisms", []):
            ring = p["ring"]
            poly = [(ring[k], ring[k + 1]) for k in range(0, len(ring), 2)]
            holes = []
            for h in p.get("holes", []):
                holes.append([(h[k], h[k + 1]) for k in range(0, len(h), 2)])
            self.prisms.append(Prism(p.get("kind"), p["base"], p["top"], poly, holes))

        # ---- bake rasters -------------------------------------------------
        g = self.G
        self.gx0 = math.floor(self.bounds[0] / g) * g
        self.gy0 = math.floor(self.bounds[2] / g) * g
        self.gnx = int((self.bounds[1] - self.gx0) / g) + 2
        self.gny = int((self.bounds[3] - self.gy0) / g) + 2
        n = self.gnx * self.gny
        self.gh = array.array("f", [0.0]) * n
        self.gmaxh = array.array("f", [0.0]) * n
        hf = self.height
        for j in range(self.gny):
            yy = self.gy0 + (j + 0.5) * g
            row = j * self.gnx
            for i in range(self.gnx):
                self.gh[row + i] = hf(self.gx0 + (i + 0.5) * g, yy)
        # cell max height from the 3x3 neighbourhood (cheap terrain occlusion)
        gnx = self.gnx
        for j in range(self.gny):
            row = j * gnx
            for i in range(gnx):
                b = row + i
                m = self.gh[b]
                if i > 0:
                    v = self.gh[b - 1]
                    if v > m: m = v
                if i + 1 < gnx:
                    v = self.gh[b + 1]
                    if v > m: m = v
                if j > 0:
                    v = self.gh[b - gnx]
                    if v > m: m = v
                if j + 1 < self.gny:
                    v = self.gh[b + gnx]
                    if v > m: m = v
                self.gmaxh[b] = m

        # Two solid rasters:
        #   self.solid      -- a cell counts as solid only when all four of its
        #                      corners are inside the footprint.  This *shrinks*
        #                      buildings, so a ray squeezing past a wall reads as
        #                      "not blocked": the bot always errs toward
        #                      assuming it is exposed.
        #   self.solid_walk -- permissive (cell centre inside), used to steer
        #                      around geometry.
        self.solid = {}
        self.solid_walk = {}
        self._cell_prism_ids = {}
        pip = _point_in_poly
        for pi, p in enumerate(self.prisms):
            x0, x1, y0, y1 = p.bbox
            i0 = max(0, int((x0 - self.gx0) / g) - 1)
            i1 = min(self.gnx - 1, int((x1 - self.gx0) / g) + 1)
            j0 = max(0, int((y0 - self.gy0) / g) - 1)
            j1 = min(self.gny - 1, int((y1 - self.gy0) / g) + 1)
            poly = p.poly
            holes = p.holes
            zr = (p.base, p.top)
            vins = {}

            def vertex(vi, vj):
                k = (vi, vj)
                v = vins.get(k)
                if v is None:
                    xx = self.gx0 + vi * g
                    yy = self.gy0 + vj * g
                    v = pip(poly, xx, yy)
                    if v:
                        for h in holes:
                            if pip(h, xx, yy):
                                v = False
                                break
                    vins[k] = v
                return v

            for j in range(j0, j1 + 1):
                yy = self.gy0 + (j + 0.5) * g
                for i in range(i0, i1 + 1):
                    xx = self.gx0 + (i + 0.5) * g
                    centre = pip(poly, xx, yy)
                    if centre:
                        for h in holes:
                            if pip(h, xx, yy):
                                centre = False
                                break
                    if not centre:
                        continue
                    self._cell_prism_ids.setdefault((i, j), []).append(pi)
                    self.solid_walk.setdefault((i, j), []).append(zr)
                    if vertex(i, j) and vertex(i + 1, j) and vertex(i, j + 1) and vertex(i + 1, j + 1):
                        self.solid.setdefault((i, j), []).append(zr)

        # trees
        tf = world["trees"]["fields"]
        data = world["trees"]["data"]
        idx = {name: i for i, name in enumerate(tf)}
        nf = len(tf)
        self.trees = []
        for i in range(0, len(data), nf):
            row = data[i:i + nf]
            self.trees.append({
                "x": row[idx["x"]], "y": row[idx["y"]], "z": row[idx["z"]],
                "height": row[idx["height"]],
                "crownR": row[idx["crownR"]],
                "crownBase": row[idx["crownBase"]],
                "trunkR": row[idx["trunkR"]],
                "crownX": row[idx.get("crownX", idx["x"])],
                "crownY": row[idx.get("crownY", idx["y"])],
            })
        self.trees_by_cell = {}
        for i, tr in enumerate(self.trees):
            r = tr["crownR"] + 2.0
            for cx in range(int((tr["x"] - r) // self.TREE_CELL), int((tr["x"] + r) // self.TREE_CELL) + 1):
                for cy in range(int((tr["y"] - r) // self.TREE_CELL), int((tr["y"] + r) // self.TREE_CELL) + 1):
                    self.trees_by_cell.setdefault((cx, cy), []).append(i)

        # Tree crowns are not baked into a raster: the conservative version
        # over-blocked rays and produced false "hidden" spots, which is fatal.
        # The exact test is `tree_blocked` below.
        self.canopy = {}

    # ------------------------------------------------------------------ height
    def height(self, x, y):
        nx, ny = self.nx, self.ny
        fx = (x - self.minX) / self.step
        fy = (y - self.minY) / self.step
        i0 = int(fx)
        j0 = int(fy)
        if i0 < 0:
            i0 = 0
        elif i0 > nx - 2:
            i0 = nx - 2
        if j0 < 0:
            j0 = 0
        elif j0 > ny - 2:
            j0 = ny - 2
        tx = fx - i0
        ty = fy - j0
        if tx < 0:
            tx = 0.0
        elif tx > 1:
            tx = 1.0
        if ty < 0:
            ty = 0.0
        elif ty > 1:
            ty = 1.0
        t = self.terrain
        b = j0 * nx + i0
        return (t[b] * (1 - tx) * (1 - ty) + t[b + 1] * tx * (1 - ty) +
                t[b + nx] * (1 - tx) * ty + t[b + nx + 1] * tx * ty) * 0.01

    def height_fast(self, x, y):
        g = self.G
        fx = (x - self.gx0) / g - 0.5
        fy = (y - self.gy0) / g - 0.5
        i0 = int(fx)
        j0 = int(fy)
        if i0 < 0:
            i0 = 0
        elif i0 > self.gnx - 2:
            i0 = self.gnx - 2
        if j0 < 0:
            j0 = 0
        elif j0 > self.gny - 2:
            j0 = self.gny - 2
        tx = fx - i0
        ty = fy - j0
        if tx < 0:
            tx = 0.0
        elif tx > 1:
            tx = 1.0
        if ty < 0:
            ty = 0.0
        elif ty > 1:
            ty = 1.0
        a = self.gh
        b = j0 * self.gnx + i0
        return (a[b] * (1 - tx) * (1 - ty) + a[b + 1] * tx * (1 - ty) +
                a[b + self.gnx] * (1 - tx) * ty + a[b + self.gnx + 1] * tx * ty)

    def slope(self, x, y, r=1.5):
        gx = (self.height_fast(x + r, y) - self.height_fast(x - r, y)) / (2 * r)
        gy = (self.height_fast(x, y + r) - self.height_fast(x, y - r)) / (2 * r)
        return math.hypot(gx, gy)

    # --------------------------------------------------------------- occlusion
    def segment_blocked(self, a, b, terrain=True, trees=False):
        """Grid-marched occlusion test from a to b (prisms, then terrain)."""
        ax, ay, az = a
        bx, by, bz = b
        dx = bx - ax
        dy = by - ay
        dz = bz - az
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return False
        g = self.G
        i = int((ax - self.gx0) / g)
        j = int((ay - self.gy0) / g)
        i1 = int((bx - self.gx0) / g)
        j1 = int((by - self.gy0) / g)
        si = 1 if dx > 0 else -1
        sj = 1 if dy > 0 else -1
        # parametric distance to next grid line
        if dx != 0:
            nx_ = (self.gx0 + (i + (1 if dx > 0 else 0)) * g - ax) / dx
            ddx = abs(g / dx)
        else:
            nx_ = 1e18
            ddx = 1e18
        if dy != 0:
            ny_ = (self.gy0 + (j + (1 if dy > 0 else 0)) * g - ay) / dy
            ddy = abs(g / dy)
        else:
            ny_ = 1e18
            ddy = 1e18
        t = 0.0
        solid = self.solid
        canopy = self.canopy if trees else None
        gh = self.gmaxh
        gnx = self.gnx
        gny = self.gny
        while True:
            key = (i, j)
            ranges = solid.get(key)
            if ranges is not None:
                pz = az + dz * t
                for lo, hi in ranges:
                    if lo < pz < hi:
                        return True
            if canopy is not None:
                crowns = canopy.get(key)
                if crowns is not None:
                    pz = az + dz * t
                    for lo, hi in crowns:
                        if lo < pz < hi:
                            return True
            if terrain:
                if 0 <= i < gnx and 0 <= j < gny:
                    if az + dz * t < gh[j * gnx + i] - 0.25:
                        return True
            if (i == i1 and j == j1) or t >= 1.0:
                break
            if nx_ < ny_:
                t = nx_
                nx_ += ddx
                i += si
            else:
                t = ny_
                ny_ += ddy
                j += sj
            if t > 1.0:
                break
            if i < -2 or i > gnx + 2 or j < -2 or j > gny + 2:
                break
        return False

    def visible(self, eye, center, top=None, terrain=True, trees=False):
        if not self.segment_blocked(eye, center, terrain, trees):
            return True
        if top is not None and not self.segment_blocked(eye, top, terrain, trees):
            return True
        return False

    def solid_at(self, x, y, z, head=0.0):
        g = self.G
        i = int((x - self.gx0) / g)
        j = int((y - self.gy0) / g)
        z1 = z + head
        for (lo, hi) in self.solid_walk.get((i, j), ()):
            if hi > z + 0.05 and lo < z1:
                return True
        return False

    # ------------------------------------------------------------------- trees
    def tree_blocked(self, a, b):
        ax, ay, az = a
        bx, by, bz = b
        x0 = min(ax, bx)
        x1 = max(ax, bx)
        y0 = min(ay, by)
        y1 = max(ay, by)
        seen = set()
        c = self.TREE_CELL
        for cx in range(int(x0 // c) - 1, int(x1 // c) + 2):
            for cy in range(int(y0 // c) - 1, int(y1 // c) + 2):
                for i in self.trees_by_cell.get((cx, cy), ()):
                    seen.add(i)
        dx = bx - ax
        dy = by - ay
        dz = bz - az
        dd = dx * dx + dy * dy + dz * dz
        if dd <= 1e-9:
            return False
        for i in seen:
            tr = self.trees[i]
            vx = tr["x"] - ax
            vy = tr["y"] - ay
            t = (vx * dx + vy * dy) / (dx * dx + dy * dy)
            if 0.0 <= t <= 1.0:
                px = ax + dx * t
                py = ay + dy * t
                pz = az + dz * t
                if math.hypot(px - tr["x"], py - tr["y"]) <= tr["trunkR"] + 0.2:
                    if tr["z"] <= pz <= tr["z"] + tr["crownBase"]:
                        return True
            cr = tr["crownR"]
            if cr <= 1.0:
                continue
            ccx, ccy = tr["crownX"], tr["crownY"]
            ccz = tr["z"] + tr["crownBase"] + cr * 0.9
            vx = ccx - ax
            vy = ccy - ay
            vz = ccz - az
            t = (vx * dx + vy * dy + vz * dz) / dd
            if t < 0.0 or t > 1.0:
                continue
            cx = ax + dx * t
            cy = ay + dy * t
            cz = az + dz * t
            if (cx - ccx) ** 2 + (cy - ccy) ** 2 + (cz - ccz) ** 2 < (cr * 0.62) ** 2:
                return True
        return False

    def tree_cover_at(self, x, y, z):
        best = 0.0
        c = self.TREE_CELL
        cx = int(x // c)
        cy = int(y // c)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.trees_by_cell.get((cx + dx, cy + dy), ()):
                    tr = self.trees[i]
                    d = math.hypot(x - tr["crownX"], y - tr["crownY"])
                    r = tr["crownR"]
                    if d < r and tr["z"] + tr["crownBase"] > z + 1.0:
                        v = 1.0 - d / r
                        if v > best:
                            best = v
        return best

    # ------------------------------------------------------------------- walks
    def walkable(self, x, y, z, max_step=1.2, max_slope_tan=1.19):
        b = self.bounds
        if x < b[0] + 1 or x > b[1] - 1 or y < b[2] + 1 or y > b[3] - 1:
            return False, z
        h = self.height_fast(x, y)
        if h - z > max_step:
            return False, h
        if h > z and self.slope(x, y) > max_slope_tan:
            return False, h
        if self.solid_at(x, y, z, head=8.6):
            return False, h
        return True, h

    def nearest_obstacle(self, x, y, z, radius):
        """Closest point on a prism the mech cannot stand in, within radius."""
        best = None
        bestd = radius
        g = self.G
        ci = int((x - self.gx0) / g)
        cj = int((y - self.gy0) / g)
        rr = int(radius / g) + 1
        seen = set()
        for j in range(cj - rr, cj + rr + 1):
            for i in range(ci - rr, ci + rr + 1):
                for idx in self._cell_prism_ids.get((i, j), ()):
                    seen.add(idx)
        for idx in seen:
            p = self.prisms[idx]
            if p.top < z + 0.5 or p.base > z + 9:
                continue
            poly = p.poly
            n = len(poly)
            for k in range(n):
                x1, y1 = poly[k]
                x2, y2 = poly[(k + 1) % n]
                px, py, d = _closest_on_seg(x, y, x1, y1, x2, y2)
                if d < bestd:
                    bestd = d
                    best = (px, py)
        return best, bestd


def _closest_on_seg(px, py, x1, y1, x2, y2):
    dx = x2 - x1
    dy = y2 - y1
    dd = dx * dx + dy * dy
    if dd <= 1e-9:
        return x1, y1, math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / dd
    if t < 0:
        t = 0.0
    elif t > 1:
        t = 1.0
    qx = x1 + dx * t
    qy = y1 + dy * t
    return qx, qy, math.hypot(px - qx, py - qy)
