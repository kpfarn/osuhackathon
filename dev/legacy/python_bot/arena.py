"""Map queries and bounded ground routing for the Mech Royale arena."""

from array import array
from collections import defaultdict
from heapq import heappop, heappush
import json
import math
from pathlib import Path
import sys


def unit(x, y):
    distance = math.hypot(x, y)
    return (x / distance, y / distance) if distance > 1e-8 else (0.0, 0.0)


def delta(a, b):
    return (a - b + math.pi) % math.tau - math.pi


def edges(flat_ring):
    points = list(zip(flat_ring[::2], flat_ring[1::2]))
    return tuple((x, y, u - x, v - y) for (x, y), (u, v) in
                 zip(points, points[1:] + points[:1]) if (x, y) != (u, v))


def inside(x, y, ring):
    hit = False
    for ax, ay, dx, dy in ring:
        if (ay > y) != (ay + dy > y) and x < ax + (y - ay) * dx / dy:
            hit = not hit
    return hit


class Prism:
    def __init__(self, data):
        self.low, self.high = data['base'], data['top']
        self.outer = edges(data['ring'])
        self.holes = tuple(edges(hole) for hole in data['holes'])
        self.edges = self.outer + tuple(edge for hole in self.holes for edge in hole)
        xs, ys = data['ring'][::2], data['ring'][1::2]
        self.bounds = min(xs), min(ys), max(xs), max(ys)

    def solid(self, x, y):
        return inside(x, y, self.outer) and not any(inside(x, y, hole) for hole in self.holes)

    def near(self, x, y, radius):
        x0, y0, x1, y1 = self.bounds
        if not (x0 - radius <= x <= x1 + radius and y0 - radius <= y <= y1 + radius):
            return False
        if self.solid(x, y):
            return True
        limit = radius * radius
        for ax, ay, dx, dy in self.edges:
            px, py = x - ax, y - ay
            t = max(0.0, min(1.0, (px * dx + py * dy) / (dx * dx + dy * dy)))
            if (px - t * dx) ** 2 + (py - t * dy) ** 2 <= limit:
                return True
        return False

    def cuts(self, start, direction):
        x, y, z = start
        dx, dy, dz = direction
        if abs(dz) < 1e-8:
            if not self.low <= z <= self.high:
                return False
            lo, hi = 0.0, 1.0
        else:
            a, b = sorted(((self.low - z) / dz, (self.high - z) / dz))
            lo, hi = max(0.0, a), min(1.0, b)
            if lo > hi:
                return False
        x0, y0, x1, y1 = self.bounds
        if (max(x + lo * dx, x + hi * dx) < x0 or min(x + lo * dx, x + hi * dx) > x1
                or max(y + lo * dy, y + hi * dy) < y0 or min(y + lo * dy, y + hi * dy) > y1):
            return False
        if self.solid(x + lo * dx, y + lo * dy):
            return True
        for ax, ay, ex, ey in self.edges:
            determinant = dx * ey - dy * ex
            if abs(determinant) < 1e-8:
                continue
            px, py = ax - x, ay - y
            t = (px * ey - py * ex) / determinant
            u = (px * dy - py * dx) / determinant
            if lo <= t <= hi and 0 <= u <= 1:
                return True
        return False


class Arena:
    CELL = 6.0
    BUCKET = 24.0
    NEIGHBORS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))

    def __init__(self, directory, stats):
        root = Path(directory) / 'v2'
        with (root / 'world.json').open() as source:
            world = json.load(source)
        bounds = world['bounds']
        self.bounds = bounds['minX'], bounds['minY'], bounds['maxX'], bounds['maxY']
        terrain = world['terrain']
        self.heights = array('h')
        self.heights.frombytes((root / terrain['file']).read_bytes())
        if sys.byteorder != 'little':
            self.heights.byteswap()
        self.tx, self.ty = terrain['minX'], terrain['minY']
        self.nx, self.ny = terrain['nx'], terrain['ny']
        self.step, self.scale = terrain['step'], terrain['scale']
        self.buckets = defaultdict(list)
        for data in world['prisms']:
            prism = Prism(data)
            x0, y0, x1, y1 = prism.bounds
            for i in range(self.bucket(x0 - 12), self.bucket(x1 + 12) + 1):
                for j in range(self.bucket(y0 - 12), self.bucket(y1 + 12) + 1):
                    self.buckets[i, j].append(prism)

        self.x0, self.y0, x1, y1 = self.bounds
        self.cols = math.ceil((x1 - self.x0) / self.CELL)
        self.rows = math.ceil((y1 - self.y0) / self.CELL)
        self.grid = bytearray(self.cols * self.rows)
        self.grid_heights = array('f', [0.0]) * len(self.grid)
        radius = stats['radius'] + 0.9
        for cell in range(len(self.grid)):
            x, y = self.center(cell)
            ground = self.ground(x, y)
            self.grid_heights[cell] = ground
            self.grid[cell] = not self.free(x, y, ground, radius, stats['height'], stats['step_up'])
        self.max_grade = math.tan(math.radians(stats['max_slope_deg']))

    def bucket(self, value):
        return math.floor(value / self.BUCKET)

    def ground(self, x, y):
        fx = min(self.nx - 1.001, max(0.0, (x - self.tx) / self.step))
        fy = min(self.ny - 1.001, max(0.0, (y - self.ty) / self.step))
        i, j = int(fx), int(fy)
        u, v = fx - i, fy - j
        index = j * self.nx + i
        heights = self.heights
        return self.scale * ((1 - v) * ((1 - u) * heights[index] + u * heights[index + 1])
                             + v * ((1 - u) * heights[index + self.nx] + u * heights[index + self.nx + 1]))

    def free(self, x, y, feet, radius, height, step):
        x0, y0, x1, y1 = self.bounds
        if not (x0 + radius <= x <= x1 - radius and y0 + radius <= y <= y1 - radius):
            return False
        for prism in self.buckets.get((self.bucket(x), self.bucket(y)), ()):
            if prism.high > feet + step and prism.low < feet + height and prism.near(x, y, radius):
                return False
        return True

    def ray(self, start, end):
        x, y, z = start
        dx, dy, dz = (end[i] - start[i] for i in range(3))
        count = max(1, math.ceil(math.hypot(dx, dy) / 3.5))
        checked = set()
        last = None
        for index in range(count + 1):
            t = index / count
            sx, sy, sz = x + t * dx, y + t * dy, z + t * dz
            if self.ground(sx, sy) > sz + 0.15:
                return False
            key = self.bucket(sx), self.bucket(sy)
            if key != last:
                for prism in self.buckets.get(key, ()):
                    if prism not in checked and prism.cuts(start, (dx, dy, dz)):
                        return False
                    checked.add(prism)
                last = key
        return True

    def cell(self, point):
        i = max(0, min(self.cols - 1, int((point[0] - self.x0) / self.CELL)))
        j = max(0, min(self.rows - 1, int((point[1] - self.y0) / self.CELL)))
        return j * self.cols + i

    def center(self, cell):
        j, i = divmod(cell, self.cols)
        return self.x0 + (i + 0.5) * self.CELL, self.y0 + (j + 0.5) * self.CELL

    def snap(self, point):
        cell = self.cell(point)
        if not self.grid[cell]:
            return cell
        row, col = divmod(cell, self.cols)
        candidates = (j * self.cols + i
                      for j in range(max(0, row - 4), min(self.rows, row + 5))
                      for i in range(max(0, col - 4), min(self.cols, col + 5))
                      if not self.grid[j * self.cols + i])
        return min(candidates, key=lambda candidate: math.dist(point[:2], self.center(candidate)), default=cell)

    def direct(self, start, goal):
        distance = math.dist(start[:2], goal[:2])
        count = max(1, math.ceil(distance / 2.5))
        return all(not self.grid[self.cell((start[0] + (goal[0] - start[0]) * i / count,
                                            start[1] + (goal[1] - start[1]) * i / count))]
                   for i in range(1, count + 1))

    def route(self, start, goal):
        source, target = self.snap(start), self.snap(goal)
        goal_x, goal_y = self.center(target)
        costs, parent = {source: 0.0}, {}
        frontier = [(0.0, 0.0, source)]
        best, best_gap = source, math.inf
        # Return the best partial path when the per-decision expansion budget is exhausted.
        for _ in range(650):
            if not frontier:
                break
            _, cost, cell = heappop(frontier)
            if cost != costs[cell]:
                continue
            x, y = self.center(cell)
            gap = math.hypot(x - goal_x, y - goal_y)
            if gap < best_gap:
                best, best_gap = cell, gap
            if cell == target:
                break
            j, i = divmod(cell, self.cols)
            for di, dj in self.NEIGHBORS:
                ni, nj = i + di, j + dj
                if not (0 <= ni < self.cols and 0 <= nj < self.rows):
                    continue
                neighbor = nj * self.cols + ni
                if self.grid[neighbor]:
                    continue
                if di and dj and (self.grid[j * self.cols + ni] or self.grid[nj * self.cols + i]):
                    continue
                distance = self.CELL * (math.sqrt(2) if di and dj else 1)
                rise = abs(self.grid_heights[neighbor] - self.grid_heights[cell])
                if rise > self.max_grade * distance:
                    continue
                next_cost = cost + distance + 0.7 * rise
                if next_cost >= costs.get(neighbor, math.inf):
                    continue
                costs[neighbor], parent[neighbor] = next_cost, cell
                heuristic = math.hypot(x + di * self.CELL - goal_x, y + dj * self.CELL - goal_y)
                heappush(frontier, (next_cost + 1.28 * heuristic, next_cost, neighbor))
        path = []
        while best != source:
            path.append(self.center(best))
            best = parent[best]
        return list(reversed(path))
