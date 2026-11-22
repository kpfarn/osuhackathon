"""Predictive aiming for the four in-game weapon types."""

from dataclasses import dataclass
import math

from arena import delta


@dataclass(frozen=True)
class Solution:
    yaw: float
    pitch: float
    distance: float
    target_radius: float
    target_height: float


def intercept(relative, velocity, speed):
    a = sum(component * component for component in velocity) - speed * speed
    b = 2.0 * sum(p * v for p, v in zip(relative, velocity))
    c = sum(component * component for component in relative)
    if abs(a) < 1e-8:
        return -c / b if b < -1e-8 else None
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return None
    root = math.sqrt(discriminant)
    times = ((-b - root) / (2 * a), (-b + root) / (2 * a))
    return min((t for t in times if t >= 0), default=None)


def dimensions(target, config):
    kind = target.get('kind')
    if kind == 'mech':
        scale = 1 + config['cubes'].get('size_per_cube', 0.05) * target.get('cubes', 0)
        return config['mech']['radius'] * scale, config['mech']['height'] * scale
    if kind == 'npc':
        return 1.8, 3.5
    return 1.5, 2.6


def angles(start, end, radius, height):
    x, y, z = (end[i] - start[i] for i in range(3))
    distance = math.hypot(x, y)
    return Solution(math.atan2(y, x), math.atan2(z, distance), distance, radius, height)


def aligned(me, solution, tick_seconds, multiplier=1.0):
    yaw_error = abs(delta(solution.yaw, me['aim'][0]))
    pitch_error = abs(solution.pitch - me['aim'][1])
    yaw_remaining = max(0.0, yaw_error - me['stats']['turn_rate'] * tick_seconds * multiplier)
    pitch_remaining = max(0.0, pitch_error - me['stats']['pitch_rate'] * tick_seconds * multiplier)
    aim_range = max(1.0, solution.distance)
    return (yaw_remaining <= math.atan2(solution.target_radius * 0.75, aim_range)
            and pitch_remaining <= math.atan2(solution.target_height * 0.28, aim_range))


def direct_solution(arena, me, target, weapon, config, tick_seconds):
    kind, stats = weapon['kind'], weapon['stats']
    radius, height = dimensions(target, config)
    position = target['pos']
    velocity = target.get('vel', (0.0, 0.0, 0.0))
    origin = tuple(me['pos'][i] + me['vel'][i] * tick_seconds for i in range(3))
    origin = origin[0], origin[1], origin[2] + me['stats']['muzzle_height']
    for fraction in (0.53, 0.76, 0.33):
        target_point = (position[0] + velocity[0] * tick_seconds,
                        position[1] + velocity[1] * tick_seconds,
                        position[2] + velocity[2] * tick_seconds + height * fraction)
        distance = math.dist(origin, target_point)
        if kind == 'lightning':
            distance = math.dist(origin[:2], target_point[:2])
        if distance > stats['range'] + (me['stats']['radius'] if kind == 'gun' else 0):
            return None
        if kind == 'gun':
            relative = tuple(target_point[i] - origin[i] for i in range(3))
            flight = intercept(relative, velocity, stats['speed'])
            if flight is None:
                return None
            # The projectile starts in front of the muzzle, shortening its flight slightly.
            flight = max(0.0, flight - (me['stats']['radius'] + 0.5) / stats['speed'])
            target_point = tuple(target_point[i] + flight * velocity[i] for i in range(3))
        solution = angles(origin, target_point, radius, height)
        if not me['stats']['min_pitch'] <= solution.pitch <= me['stats']['max_pitch']:
            continue
        if arena.ray(origin, target_point):
            return solution
    return None


def mortar_solution(arena, me, target, weapon, config, tick_seconds):
    stats = weapon['stats']
    radius, height = dimensions(target, config)
    origin = (me['pos'][0], me['pos'][1], me['pos'][2] + me['stats']['height'] + 0.5)
    distance = math.dist(origin[:2], target['pos'][:2])
    if not 14 <= distance <= 115:
        return None
    vx, vy, vz = target.get('vel', (0.0, 0.0, 0.0))
    px, py, pz = (target['pos'][i] - origin[i] for i in range(3))
    pz += height * 0.4
    gravity, speed = stats['gravity'], stats['speed']

    def displacement(t):
        return px + vx * t, py + vy * t, pz + vz * t + 0.5 * gravity * t * t

    low = 0.05
    for step in range(1, 20):
        high = step * 0.13
        if math.hypot(*displacement(high)) <= speed * high:
            break
        low = high
    else:
        return None
    for _ in range(9):
        mid = (low + high) / 2
        if math.hypot(*displacement(mid)) <= speed * mid:
            high = mid
        else:
            low = mid
    x, y, z = displacement(high)
    pitch = math.atan2(z, math.hypot(x, y))
    if not me['stats']['min_pitch'] <= pitch <= me['stats']['max_pitch']:
        return None
    solution = Solution(math.atan2(y, x), pitch, distance, radius + 0.8, height + 1.0)
    if not aligned(me, solution, tick_seconds * 3):
        return solution
    last = origin
    for index in range(1, 7):
        fraction = index / 6
        p = (origin[0] + x * fraction, origin[1] + y * fraction,
             origin[2] + z * fraction - 0.5 * gravity * (high * fraction) ** 2)
        if not arena.ray(last, p):
            return None
        last = p
    return solution


def aim_for(arena, me, target, weapon, config, tick_seconds):
    if weapon['kind'] == 'mortar':
        return mortar_solution(arena, me, target, weapon, config, tick_seconds)
    return direct_solution(arena, me, target, weapon, config, tick_seconds)
