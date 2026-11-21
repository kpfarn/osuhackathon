"""A placement-first bot for the official Mech Royale simulator."""

from collections import deque
import math

import mechfight as mf
from arena import Arena, delta, unit
from combat import aim_for, aligned, dimensions


class Bot:
    name = 'Cover Circuit'
    WEAPON_VALUE = {'gun': 0, 'mortar': 1, 'lightning': 2, 'laser': 3}
    UPGRADE_VALUE = {
        'nanites': 12, 'servos': 10, 'armor': 9.5, 'plating': 9,
        'gun_mag': 8.5, 'jets': 7.5, 'gun_damage': 7, 'laser_charge': 7,
        'reactor': 6.5, 'gun_rate': 6.5, 'gun_velocity': 6, 'capacitor': 5,
        'laser_damage': 6, 'lightning_efficiency': 6, 'lightning_range': 6,
        'gyros': 5, 'sensors': 4.5, 'mortar_reload': 4, 'mortar_splash': 4,
    }

    def __init__(self, hello):
        self.config = hello['config']
        self.arena = Arena(hello['map']['dir'], self.config['mech'])
        self.tick_seconds = 1 / self.config['match']['tick_hz']
        self.damage = deque()
        self.motion = deque()
        self.path = deque()
        self.route_goal = None
        self.route_until = 0.0
        self.cover_goal = None
        self.cover_until = 0.0
        self.retreat_until = 0.0
        self.used_recovery_shot = False
        self.recover_until = 0.0
        self.boosting = False
        self.last_move = (0.0, 0.0)
        self.target_id = None
        self.resources = {}
        self.resource_id = None
        self.resource_best = math.inf
        self.resource_deadline = 0.0
        self.retry_after = {}
        self.patrol_point = None
        self.patrol_until = 0.0
        self.capture_sample = None
        self.edge_item = None
        self.edge_until = 0.0
        self.edge_return = None
        self.turn = 1 if hello['slot'] % 2 == 0 else -1

    @staticmethod
    def zone_margin(point, center, radius):
        return radius - math.dist(point[:2], center)

    def zone_goal(self, obs):
        me, zone, now = obs['self'], obs['zone'], obs['t']
        pos, center, radius = me['pos'], zone['next_center'], zone['next_radius']
        if now >= zone['shrink_end']:
            center, radius = zone['center'], zone['radius']
        margin = self.zone_margin(pos, center, radius)
        current_margin = self.zone_margin(pos, zone['center'], zone['radius'])
        buffer = min(32.0, radius * 0.48)
        eta = max(0.0, buffer - margin) / max(1, me['stats']['walk_speed'])
        urgent = (not me['in_zone'] or current_margin < min(12, zone['radius'] * 0.3)
                  or (margin < buffer and zone['shrink_end'] - now < eta * 1.55 + 11))
        ux, uy = unit(pos[0] - center[0], pos[1] - center[1])
        depth = max(0.0, radius - (max(buffer + 18, radius * 0.30) if not urgent else buffer + 10))
        return (center[0] + ux * depth, center[1] + uy * depth), urgent

    def open_ground(self, obj, me):
        x, y, z = obj['pos']
        return (abs(z - self.arena.ground(x, y)) < 3.5
                or (abs(z - me['pos'][2]) < 3.5 and self.arena.ground(x, y) + 5 < z))

    def remember_resources(self, obs):
        """Static pickups remain useful after the torso turns away."""
        now, pos = obs['t'], obs['self']['pos']
        seen = set()
        for field, kind in (('pickups', 'weapon'), ('cubes', 'cube'), ('crates', 'crate')):
            for obj in obs[field]:
                self.resources[obj['id']] = (kind, obj, now)
                seen.add(obj['id'])
        for ident, (_, obj, last) in list(self.resources.items()):
            reached = math.dist(pos, obj['pos']) < 7 and ident not in seen
            if reached or now - last > 9:
                del self.resources[ident]
        for event in obs['events']:
            if event['type'] == 'took' or (event['type'] == 'take_failed' and event['reason'] == 'gone'):
                self.resources.pop(event['id'], None)

    def secondary_values(self, obs):
        """Prefer lightning when the available space is closing to arc range."""
        me, zone = obs['self'], obs['zone']
        arc_range = self.config['weapons']['lightning']['range']
        held = {w['kind'] for w in me['weapons'] if w}
        seconds_left = zone['shrink_end'] - obs['t']
        close_ring = (zone['radius'] <= arc_range or
                      (zone['next_radius'] <= arc_range and zone['radius'] <= 1.6 * arc_range
                       and 0 < seconds_left <= 12))
        if not close_ring:
            return self.WEAPON_VALUE
        energy_ready = me['energy'] >= 0.40 * me['max_energy']
        if not energy_ready and 'lightning' not in held:
            return self.WEAPON_VALUE
        laser_investment = me['upgrades'].count('laser_damage') + me['upgrades'].count('laser_charge')
        arc_investment = sum(u.startswith('lightning_') for u in me['upgrades'])
        if 'laser' in held and laser_investment >= arc_investment + 2:
            return self.WEAPON_VALUE
        return {'gun': 0, 'mortar': 1, 'laser': 2, 'lightning': 3}

    def edge_weapon(self, obs, nearest, retreating):
        """A short round trip for a visible early pickup, while the circle is stationary."""
        me, zone, now = obs['self'], obs['zone'], obs['t']
        pos, speed = me['pos'], me['stats']['walk_speed']
        values = self.secondary_values(obs)
        current = max((values[w['kind']] for w in me['weapons'] if w), default=0)
        active = self.edge_item
        self.edge_item = None
        if (zone['dps'] > 35 or zone['shrink_start'] - now < 5 or retreating
                or nearest < 100 or self.damage or obs['projectiles']
                or any(s['dist'] == 'near' for s in obs['sounds'])
                or me['hp'] < 0.8 * me['max_hp']
                or (active is None and (not me['in_zone'] or self.edge_return is not None))):
            return None
        if active is not None and now >= self.edge_until:
            return None
        picks = obs['pickups']
        best, best_cost = None, math.inf
        for pickup in picks:
            kind, ident, point = pickup['kind'], pickup['id'], pickup['pos']
            if active is not None and ident != active:
                continue
            if active is None and self.retry_after.get(ident, 0) > now:
                continue
            if values[kind] < 2 or values[kind] - current < 2:
                continue
            depth = -self.zone_margin(point, zone['center'], zone['radius'])
            distance = math.dist(pos[:2], point[:2])
            if not 0 < depth <= 9 or distance > 20 or not self.open_ground(pickup, me):
                continue
            if abs(pos[2] - point[2]) > 3 or not self.arena.direct(pos, point):
                continue
            # Both legs use walking speed. Budget time for the turn and a delayed pickup.
            inward = unit(zone['center'][0] - point[0], zone['center'][1] - point[1])
            back = tuple(point[i] + inward[i] * (depth + 9) for i in range(2))
            if not self.arena.direct(point, back):
                continue
            duration = (distance + depth + 9) / speed + 1.0
            # Charge the entire trip as exposure, even the part spent inside.
            loss = duration * zone['dps']
            if duration >= zone['shrink_start'] - now or loss > min(85, 0.065 * me['hp']):
                continue
            if any(values[p['kind']] >= values[kind] and p['id'] != ident
                   and self.zone_margin(p['pos'], zone['center'], zone['radius']) >= 3
                   and math.dist(pos, p['pos']) <= 35 and self.open_ground(p, me)
                   and self.arena.direct(pos, p['pos']) for p in picks):
                continue
            cost = loss - 18 * values[kind]
            if cost < best_cost:
                best, best_cost = pickup, cost
                self.edge_return = back
        if best:
            self.edge_item = best['id']
            if active is None:
                self.edge_until = now + 3.5
                self.retry_after[self.edge_item] = now + 14
        return best

    def arrival_margin(self, obs, point, seconds):
        zone, now = obs['zone'], obs['t']
        start = max(now, zone['shrink_start'])
        end = zone['shrink_end']
        if end <= now:
            return self.zone_margin(point, zone['center'], zone['radius'])
        fraction = min(1.0, max(0.0, (now + seconds - start) / max(0.001, end - start)))
        center = [a + (b - a) * fraction for a, b in zip(zone['center'], zone['next_center'])]
        radius = zone['radius'] + fraction * (zone['next_radius'] - zone['radius'])
        return self.zone_margin(point, center, radius)

    def loot_goal(self, obs, enemy_distance):
        me, zone, now = obs['self'], obs['zone'], obs['t']
        pos = me['pos']
        values = self.secondary_values(obs)
        equipped = max((values[w['kind']] for w in me['weapons'] if w), default=0)
        candidates = []
        early = me['cubes'] < 3
        cube_rules = self.config['cubes']
        next_cube_unlocks = me['cubes'] + 1 in cube_rules['upgrade_thresholds']
        capped = me['cubes'] >= cube_rules['stat_cap']
        gearing_up = early and equipped > 0 and enemy_distance > 100
        for ident, (kind, obj, last) in self.resources.items():
            if self.retry_after.get(ident, 0) > now or not self.open_ground(obj, me):
                continue
            distance = math.dist(pos[:2], obj['pos'][:2])
            if kind == 'weapon':
                value = values[obj['kind']]
                if value <= equipped:
                    continue
                limit = 160 if equipped == 0 else 78
                reward = 72 + 10 * value if equipped == 0 else 40
            elif kind == 'cube':
                limit, reward = (100, 84) if early else (75, 63)
                if capped:
                    # Spare cubes still increase collision size. A nearby upgrade can justify one.
                    if not next_cube_unlocks:
                        continue
                    limit, reward = 32, 56
                elif gearing_up and next_cube_unlocks:
                    limit, reward = 125, 106
                elif not early and next_cube_unlocks and enemy_distance > 100:
                    limit, reward = 92, 83
            else:
                if me['cubes'] >= 6 or enemy_distance < 85:
                    continue
                limit, reward = (107, 47) if early else (70, 25)
                if gearing_up:
                    limit, reward = 130, 65 if next_cube_unlocks else 54
            if enemy_distance < 65:
                limit = min(limit, 15)
            if distance > limit:
                continue
            cost = distance - reward + 2 * (now - last)
            if ident == self.resource_id:
                cost -= 19
            candidates.append((cost, kind, obj))
        for drop in obs.get('drops', ()):
            distance = math.dist(pos[:2], drop['pos'][:2])
            if (enemy_distance >= 64 and drop['holder'] in (None, me['id'])
                    and distance <= 150 and drop['lands_in'] < distance / me['stats']['walk_speed'] + 3):
                healing = 55 * (1 - me['hp'] / me['max_hp'])
                candidates.append((distance - 93 - healing - drop['progress'] * 10, 'drop', drop))

        # Prefer a reachable item inside the next ring; rule out detours with no time to return.
        best, best_cost = (None, None), math.inf
        enemies = [actor for actor in obs['mechs'] if actor['kind'] == 'mech']
        for cost, kind, obj in candidates:
            point = obj['pos']
            if self.zone_margin(point, zone['center'], zone['radius']) < 3:
                continue
            distance = math.dist(pos[:2], point[:2])
            outside = max(0.0, -self.zone_margin(point, zone['next_center'], zone['next_radius']))
            travel = distance * (1.0 if self.arena.direct(pos, point) else 1.45)
            if outside and zone['dps'] >= 60:
                if kind == 'drop':
                    dwell = max(0, obj['lands_in'] - travel / me['stats']['walk_speed']) + obj['capture_s'] - obj['progress']
                elif kind == 'crate':
                    dwell = 3.0
                else:
                    dwell = 0.6
                if self.arrival_margin(obs, point, travel / me['stats']['walk_speed'] + dwell + 2.0) < 5:
                    continue
            if outside and (travel + outside) / me['stats']['walk_speed'] + 5 >= zone['shrink_end'] - now:
                continue
            contested = min((math.dist(point[:2], e['pos'][:2]) for e in enemies), default=math.inf)
            if distance > 16 and contested < min(42, distance):
                continue
            cost += travel - distance + 0.36 * outside + max(0, 64 - contested)
            if cost < best_cost:
                best_cost, best = cost, (kind, obj)
        return best

    def finishing_drop(self, obs, enemies):
        """Keep a nearly finished capture only while its clock is advancing safely."""
        me, now = obs['self'], obs['t']
        drop = next((d for d in obs['drops'] if d['holder'] == me['id'] and d['lands_in'] <= 0), None)
        if drop is None:
            self.capture_sample = None
            return None
        remaining = drop['capture_s'] - drop['progress']
        if not self.capture_sample or self.capture_sample[:2] != (drop['id'], drop['progress']):
            self.capture_sample = drop['id'], drop['progress'], now
        if not 0 < remaining <= 1.2 or now - self.capture_sample[2] >= 0.35:
            return None
        point, radius = drop['pos'], drop['radius']
        if (not me['in_zone'] or math.dist(me['pos'][:2], point[:2]) > radius
                or self.arrival_margin(obs, me['pos'], remaining + 1.5) < 4
                or self.arrival_margin(obs, point, remaining + 1.5) < 4):
            return None
        zone = obs['zone']
        outside_next = -self.zone_margin(me['pos'], zone['next_center'], zone['next_radius'])
        if outside_next > 0:
            walk = outside_next / me['stats']['walk_speed']
            if remaining + 1.55 * walk + 3 >= zone['shrink_end'] - now:
                return None
        # A competitor entering the ring freezes the clock. Leave a speed and reaction margin.
        contest_radius = radius + (remaining + 0.4) * self.config['mech']['boost_speed'] * 1.3
        if any(math.dist(e['pos'][:2], point[:2]) <= contest_radius for e in enemies):
            return None
        if self.hazards(obs) or sum(d[1] for d in self.damage if now - d[0] <= 1.0) > 0.12 * me['hp']:
            return None
        return drop

    def recovery_drop(self, obs, enemies):
        """Use a nearby supply ring only if it can finish before contact or zone damage."""
        me, zone = obs['self'], obs['zone']
        pos, stats = me['pos'], me['stats']
        if me['hp'] > 0.70 * me['max_hp'] or self.damage or obs['projectiles']:
            return None
        if any(sound['dist'] == 'near' and sound['kind'] in
               ('gun', 'mortar', 'explosion', 'laser', 'laser_charge', 'lightning', 'npc_gun')
               for sound in obs['sounds']):
            return None
        best, earliest = None, math.inf
        for drop in obs.get('drops', ()):
            if drop['holder'] not in (None, me['id']):
                continue
            point, radius = drop['pos'], drop['radius']
            distance = math.dist(pos[:2], point[:2])
            if distance > 36 or not self.arena.direct(pos, point):
                continue
            if self.zone_margin(point, zone['next_center'], zone['next_radius']) <= radius:
                continue
            walk = max(0.0, distance - radius * 0.5) / stats['walk_speed']
            banked = drop['progress'] if drop['holder'] == me['id'] else 0
            finish = max(walk, drop['lands_in']) + max(0, drop['capture_s'] - banked)
            if finish > 7 or finish >= earliest:
                continue
            safe_distance = radius + (finish + 1.0) * self.config['mech']['boost_speed']
            if any(math.dist(enemy['pos'][:2], point[:2]) < safe_distance for enemy in enemies):
                continue
            best, earliest = drop, finish
        return best

    def patrol_goal(self, obs, zone_goal):
        pos, zone, now = obs['self']['pos'], obs['zone'], obs['t']
        center, radius = zone['next_center'], zone['next_radius']
        if radius < 15:
            return tuple(center)
        if self.zone_margin(pos, center, radius) < min(18, radius * 0.25):
            self.patrol_point = None
            return zone_goal
        if (self.patrol_point is not None and now < self.patrol_until
                and math.dist(pos[:2], self.patrol_point) > 7
                and self.zone_margin(self.patrol_point, center, radius) > 2):
            return self.patrol_point
        # Follow a short arc inside the next ring instead of switching between center and rim.
        heading = math.atan2(pos[1] - center[1], pos[0] - center[0])
        orbit = min(radius * 0.78, max(radius * 0.38, math.dist(pos[:2], center)))
        step = min(1.4, 70 / max(10, orbit))
        for offset in (step, step * 0.55, -step):
            angle = heading + self.turn * offset
            point = center[0] + orbit * math.cos(angle), center[1] + orbit * math.sin(angle)
            cell = self.arena.snap(point)
            candidate = self.arena.center(cell)
            if (not self.arena.grid[cell] and self.zone_margin(candidate, center, radius) > 2
                    and math.dist(pos[:2], candidate) > 7):
                self.patrol_point, self.patrol_until = candidate, now + 10
                return candidate
        return zone_goal

    def loot_action(self, obs, action):
        me = obs['self']
        slots = me['weapons']
        values = self.secondary_values(obs)
        held = {weapon['kind'] for weapon in slots if weapon}
        radius = self.config['loot']['pickup_radius']
        candidates = [pickup for pickup in obs['pickups']
                      if pickup['kind'] not in held and abs(pickup['pos'][2] - me['pos'][2]) <= 4
                      and math.dist(pickup['pos'][:2], me['pos'][:2]) <= radius]
        if candidates:
            pickup = max(candidates, key=lambda item: values[item['kind']])
            if None in slots and pickup['kind'] != 'gun':
                action['take'] = pickup['id']
            elif None not in slots:
                secondary = next((i for i, weapon in enumerate(slots) if weapon['kind'] != 'gun'), None)
                if secondary is not None:
                    weapon = slots[secondary]
                    if values[pickup['kind']] > values[weapon['kind']] and not weapon.get('charge', 0):
                        action.update(take=pickup['id'], drop=secondary)
        offer = obs.get('offer')
        if offer and offer.get('choices'):
            choices = offer['choices']
            value = self.UPGRADE_VALUE
            action['pick'] = max(range(len(choices)), key=lambda i: value.get(choices[i]['id'], 3))

    def update_damage(self, obs):
        now = obs['t']
        for event in obs['events']:
            if event['type'] == 'damaged' and event['weapon'] != 'zone':
                self.damage.append((now, event['amount'], event['bearing'], event['by']))
        while self.damage and self.damage[0][0] < now - 2.5:
            self.damage.popleft()

    def retreat_goal(self, obs, enemies, zone_goal):
        me, now, zone = obs['self'], obs['t'], obs['zone']
        pos = me['pos']
        if self.cover_goal is not None and now < self.cover_until:
            return self.cover_goal
        candidates = [(pos[0], pos[1]), zone_goal]
        for index in range(12):
            yaw = index * math.tau / 12
            for distance in (15, 36):
                candidates.append((pos[0] + distance * math.cos(yaw), pos[1] + distance * math.sin(yaw)))
        threats = enemies[:2]
        damage_bearing = self.damage[-1][2] if self.damage else None
        if not threats and damage_bearing is not None:
            threats = [{'pos': [pos[0] + 85 * math.cos(damage_bearing),
                                pos[1] + 85 * math.sin(damage_bearing), pos[2]]}]
        origins = [(actor['pos'][0], actor['pos'][1], actor['pos'][2] + 7) for actor in threats]
        danger_margin = min(9, zone['radius'] * 0.25)
        best, best_score = zone_goal, -math.inf
        for x, y in candidates:
            margin = self.zone_margin((x, y), zone['center'], zone['radius'])
            if margin < danger_margin or self.arena.grid[self.arena.cell((x, y))]:
                continue
            distance = math.dist((x, y), pos[:2])
            ground = self.arena.ground(x, y)
            covered = sum(not self.arena.ray(origin, (x, y, ground + 5.5)) for origin in origins)
            separation = min((math.dist((x, y), origin[:2]) for origin in origins), default=0)
            score = 54 * covered + 0.38 * separation - 0.45 * distance
            if origins and covered < len(origins) and separation < 40:
                score -= (40 - separation) * 1.5
            if distance and not self.arena.direct(pos, (x, y)):
                score -= 12
            if score > best_score:
                best, best_score = (x, y), score
        self.cover_goal, self.cover_until = best, now + 0.9
        return best

    def combat_target(self, obs, enemies, crate, retreating):
        me, pos = obs['self'], obs['self']['pos']
        weapons = me['weapons']
        if retreating:
            # Allow one arm-gun trigger during this recovery episode. Movement still seeks cover.
            if self.used_recovery_shot or len(enemies) != 1:
                return None, None, None
            enemy = enemies[0]
            if any(event[3] != enemy['id'] for event in self.damage):
                return None, None, None
            gun = weapons[0]  # This policy always keeps the starting gun in slot zero.
            if not gun or gun['kind'] != 'gun' or not gun['ammo'] or gun['reload'] or gun['cooldown'] > 0:
                return None, None, None
            cubes = self.config['cubes']
            scale = 1 + cubes['damage_per_cube'] * min(me['cubes'], cubes['stat_cap'])
            # Reserve 25% for unknown armor. Unknown enemy HP never qualifies.
            if enemy.get('hp', math.inf) > 0.75 * gun['stats']['damage'] * scale:
                return None, None, None
            solution = aim_for(self.arena, me, enemy, gun, self.config, self.tick_seconds)
            if solution and aligned(me, solution, 0.0):
                self.used_recovery_shot = True
                return enemy, 0, solution
            return None, None, None
        ranked = sorted(enemies, key=lambda actor:
                        math.dist(pos[:2], actor['pos'][:2])
                        + 0.025 * actor.get('hp', actor.get('max_hp', 1400))
                        - (13 if actor['id'] == self.target_id else 0))
        if crate is not None:
            ranked.append(crate)
        drones = [actor for actor in obs['mechs'] if actor['kind'] == 'npc'
                  and math.dist(pos[:2], actor['pos'][:2]) < 42]
        nearest_drone = min(drones, key=lambda actor: math.dist(pos[:2], actor['pos'][:2]), default=None)
        if nearest_drone is not None:
            ranked.append(nearest_drone)
        for target in ranked[:4]:
            distance = math.dist(pos[:2], target['pos'][:2])
            reach = 170
            if (target.get('kind') == 'mech' and me['in_zone'] and obs['zone']['radius'] >= 140
                    and self.zone_margin(pos, obs['zone']['center'], obs['zone']['radius']) > 32
                    and self.zone_margin(pos, obs['zone']['next_center'], obs['zone']['next_radius']) > 10):
                reach = max((w['stats']['range'] for w in weapons if w and w['kind'] == 'laser'), default=170)
            if distance > reach or (target.get('kind') not in ('mech', 'npc') and distance > 60):
                continue
            order = []
            for index, weapon in enumerate(weapons):
                if not weapon:
                    continue
                kind = weapon['kind']
                ready = weapon['cooldown'] <= 0.11
                if kind == 'laser' and weapon.get('charge', 0) > 0:
                    priority = 8
                elif kind == 'laser' and ready and (distance > 29 or not target.get('kind')):
                    priority = 5
                elif kind == 'lightning' and ready and me['energy'] >= weapon['stats']['energy'] + 7:
                    priority = 4
                elif kind == 'mortar' and ready and target.get('kind') == 'mech':
                    priority = 3
                elif kind == 'gun' and distance <= 170:
                    priority = 2
                else:
                    continue
                order.append((priority, index, weapon))
            for _, index, weapon in sorted(order, reverse=True):
                solution = aim_for(self.arena, me, target, weapon, self.config, self.tick_seconds)
                if solution is not None:
                    return target, index, solution
        return None, None, None

    def fire_control(self, obs, action, target, index, solution):
        if target is None:
            self.target_id = None
            return
        me = obs['self']
        weapon = me['weapons'][index]
        stats = weapon['stats']
        self.target_id = target['id']
        action['aim'] = [solution.yaw, solution.pitch]
        ready = aligned(me, solution, self.tick_seconds)
        kind = weapon['kind']
        if kind == 'laser':
            charge = weapon.get('charge', 0)
            release = max(stats['min_charge'], stats['charge'] - 0.045)
            if solution.distance < 35:
                release = max(stats['min_charge'], stats['charge'] * 0.65)
            action['fire'][index] = (charge < release or not ready) if charge else (
                weapon['cooldown'] <= 0.06 and aligned(me, solution, 0.30))
        elif kind == 'lightning':
            action['fire'][index] = abs(delta(solution.yaw, me['aim'][0])) <= math.radians(stats['cone_deg'] * 0.46)
        else:
            action['fire'][index] = ready

        if kind in ('laser', 'lightning'):
            gun_index = next((i for i, w in enumerate(me['weapons']) if w and w['kind'] == 'gun'), None)
            if gun_index is not None and (kind == 'lightning' or not action['fire'][index]):
                gun = me['weapons'][gun_index]
                gun_aim = aim_for(self.arena, me, target, gun, self.config, self.tick_seconds)
                if gun_aim and aligned(me, gun_aim, 0.0):
                    # Both weapons share the same torso. Fire together only when the lead angles agree.
                    radius, _ = dimensions(target, self.config)
                    allowance = math.atan2(radius * 0.55, max(1.0, gun_aim.distance))
                    if math.hypot(delta(gun_aim.yaw, solution.yaw), gun_aim.pitch - solution.pitch) < allowance:
                        action['fire'][gun_index] = True

    def waypoint(self, obs, goal):
        pos, now = obs['self']['pos'], obs['t']
        if self.arena.direct(pos, goal):
            self.path.clear()
            self.route_goal = None
            return goal
        if self.route_goal is None or math.dist(goal, self.route_goal) > 11 or now >= self.route_until:
            self.path = deque(self.arena.route(pos, goal))
            self.route_goal, self.route_until = goal, now + 0.9
        while self.path and math.dist(pos[:2], self.path[0]) < 4.0:
            self.path.popleft()
        if self.path:
            for waypoint in reversed(list(self.path)[:5]):
                if self.arena.direct(pos, waypoint):
                    return waypoint
            return self.path[0]
        return goal

    def hazards(self, obs):
        pos = obs['self']['pos']
        nearby = []
        for shot in obs['projectiles']:
            dx, dy, dz = (shot['pos'][i] - pos[i] for i in range(3))
            vx, vy, vz = shot['vel']
            horizontal = math.hypot(vx, vy)
            if math.hypot(dx, dy) > horizontal * 0.8 + 15:
                continue
            eta = -(dx * vx + dy * vy) / max(1.0, horizontal * horizontal)
            gap = math.hypot(dx + eta * vx, dy + eta * vy)
            if shot['kind'] == 'mortar' or (-0.05 < eta < 0.75 and gap < 16):
                nearby.append((gap, (dx, dy, dz, vx, vy, vz, shot['kind'] == 'mortar')))
        nearby.sort(key=lambda shot: shot[0])
        return [shot for _, shot in nearby[:12]]

    def projectile_penalty(self, shots, move, speed, stats):
        penalty = 0.0
        for dx, dy, z, vx, vy, vz, mortar in shots:
            rx, ry = vx - speed * move[0], vy - speed * move[1]
            time = -(dx * rx + dy * ry) / max(1.0, rx * rx + ry * ry)
            if not 0 < time < 0.64:
                continue
            height = z + time * vz
            if mortar:
                height -= 0.5 * self.config['weapons']['mortar']['gravity'] * time * time
            if not -2 < height < stats['height'] + 2:
                continue
            gap = math.hypot(dx + time * rx, dy + time * ry)
            radius = stats['radius'] + (7 if mortar else 1.1)
            penalty += max(0.0, (radius - gap) / radius) * (7.0 if mortar else 3.5)
        return penalty

    def steer(self, obs, desired, running, enemies, hold, edge_trip=False):
        me, zone = obs['self'], obs['zone']
        x, y, z = me['pos']
        stats = me['stats']
        speed = stats['boost_speed'] if running and me['grounded'] else stats['walk_speed']
        shots = self.hazards(obs)
        yaw = math.atan2(desired[1], desired[0])
        if math.hypot(*desired) < 0.01:
            if not shots:
                return 0.0, 0.0
            yaw = me['aim'][0]
        # Pick nearby headings before committing to them. The route handles larger detours.
        candidates = [(math.cos(yaw + offset), math.sin(yaw + offset))
                      for offset in (0, 0.35, -0.35, 0.7, -0.7, 1.15, -1.15, 1.57, -1.57, 2.25, -2.25, math.pi)]
        candidates.append((0.0, 0.0))
        current_margin = self.zone_margin((x, y), zone['center'], zone['radius'])
        start_ground = self.arena.ground(x, y)
        threats = [enemy['pos'][:2] for enemy in enemies[:2] if math.dist((x, y), enemy['pos'][:2]) < 60]
        best, best_score = (0.0, 0.0), -math.inf
        for mx, my in candidates:
            score = 2.6 * (mx * desired[0] + my * desired[1])
            score += 0.32 * (mx * self.last_move[0] + my * self.last_move[1])
            if not (mx or my):
                score += 1.0 if hold else -1.1
            px, py = x + mx * speed * 0.55, y + my * speed * 0.55
            margin = self.zone_margin((px, py), zone['center'], zone['radius'])
            if not edge_trip and margin < min(5, current_margin):
                score -= 0.85 * (min(5, current_margin) - margin)
            if not edge_trip and current_margin < 0:
                score += 0.26 * (margin - current_margin)
            for fraction in (0.35, 0.68, 1.0) if mx or my else ():
                qx, qy = x + fraction * (px - x), y + fraction * (py - y)
                floor = self.arena.ground(qx, qy)
                feet = max(z, floor)
                allowed_rise = max(stats['step_up'], math.dist((x, y), (qx, qy)) * 0.68)
                if (floor > max(z, start_ground) + allowed_rise
                        or not self.arena.free(qx, qy, feet, stats['radius'] + 0.15, stats['height'], stats['step_up'])):
                    score -= 8 * (1.5 - fraction)
                    break
            for threat in threats:
                old_gap = math.dist((x, y), threat)
                new_gap = math.dist((px, py), threat)
                if new_gap < min(old_gap, 29):
                    score -= 0.18 * (min(old_gap, 29) - new_gap)
            score -= self.projectile_penalty(shots, (mx, my), speed, stats)
            if score > best_score:
                best, best_score = (mx, my), score
        return best

    def act(self, obs):
        try:
            return self._act(obs)
        except Exception:
            # a single unexpected observation must not end the match
            me = obs.get('self') or {}
            aim = me.get('aim') or (0.0, 0.0)
            move = [0.0, 0.0]
            try:
                zone = obs.get('zone') or {}
                center = zone.get('center') or (0.0, 0.0)
                pos = me.get('pos') or (0.0, 0.0, 0.0)
                dx, dy = center[0] - pos[0], center[1] - pos[1]
                d = math.hypot(dx, dy)
                if d > 1e-6:
                    move = [dx / d, dy / d]
            except Exception:
                pass
            return {'tick': obs.get('tick', 0), 'move': move, 'boost': False,
                    'jump': False, 'aim': [float(aim[0]), float(aim[1])],
                    'fire': [False, False]}

    def _act(self, obs):
        me, now, zone = obs['self'], obs['t'], obs['zone']
        pos, stats = me['pos'], me['stats']
        hp = me['hp'] / max(1, me['max_hp'])
        enemies = sorted((actor for actor in obs['mechs'] if actor['kind'] == 'mech'),
                         key=lambda actor: math.dist(pos[:2], actor['pos'][:2]))
        nearest = math.dist(pos[:2], enemies[0]['pos'][:2]) if enemies else math.inf
        close_count = sum(math.dist(pos[:2], enemy['pos'][:2]) < 63 for enemy in enemies)
        if now >= self.retreat_until:
            self.used_recovery_shot = False
        self.update_damage(obs)
        damage = sum(event[1] for event in self.damage)
        if ((hp < 0.59 and (nearest < 115 or damage > 0))
                or (close_count >= 2 and damage > 0.085 * me['max_hp'] and hp < 0.88)
                or (hp < 0.46 and now < self.retreat_until)):
            self.retreat_until = now + 2.6
        retreating = now < self.retreat_until
        zone_goal, urgent = self.zone_goal(obs)
        self.remember_resources(obs)
        kind, loot = None, None
        edge = self.edge_weapon(obs, nearest, retreating) if urgent or self.edge_item is not None else None
        returning = (edge is None and self.edge_return is not None and now <= self.edge_until + 3
                     and self.zone_margin(pos, zone['center'], zone['radius']) < 6
                     and self.zone_margin(self.edge_return, zone['center'], zone['radius']) >= 6)
        if returning:
            urgent, zone_goal = True, self.edge_return
        elif edge is None:
            self.edge_return = None
        finish = self.finishing_drop(obs, enemies)
        if finish is not None and not returning and edge is None:
            kind, loot = 'drop', finish
            urgent = False
        elif edge is not None:
            kind, loot = 'edge', edge
        elif not urgent:
            if retreating:
                loot = self.recovery_drop(obs, enemies)
                if loot is not None:
                    kind = 'drop'
            else:
                kind, loot = self.loot_goal(obs, nearest)
        if loot is not None and kind != 'drop':
            distance = math.dist(pos[:2], loot['pos'][:2])
            ident = loot['id']
            hit = any(e['type'] == 'hit' and e['target'] == ident for e in obs['events'])
            if ident != self.resource_id or distance < self.resource_best - 3 or hit:
                self.resource_best, self.resource_deadline = distance, now + 7
            elif now >= self.resource_deadline:
                self.retry_after[ident] = now + 12
                kind, loot = None, None
        self.resource_id = loot['id'] if loot else None
        crate = next((c for c in obs['crates'] if c['id'] == self.resource_id), None)

        goal, hold = zone_goal, False
        target, slot, solution = self.combat_target(obs, enemies, crate, retreating and not urgent)
        if kind == 'edge':
            mode, goal = 'edge', tuple(loot['pos'][:2])
        elif urgent:
            mode = 'rotate'
        elif retreating and kind != 'drop':
            mode = 'recover'
            goal = self.retreat_goal(obs, enemies, zone_goal)
            hold = math.dist(pos[:2], goal) < 4.3
        elif kind and kind != 'crate':
            mode = kind
            goal = tuple(loot['pos'][:2])
            hold = kind == 'drop' and math.dist(pos[:2], goal) < loot['radius'] * 0.60
        elif target is not None and target.get('kind') == 'mech':
            mode = 'engage'
            vx, vy = unit(target['pos'][0] - pos[0], target['pos'][1] - pos[1])
            distance = math.dist(pos[:2], target['pos'][:2])
            preferred = 39 if target.get('hp', me['max_hp']) < 0.5 * me['max_hp'] else 57
            if any(weapon and weapon['kind'] == 'lightning' for weapon in me['weapons']):
                preferred -= 9
            radial = max(-1.0, min(0.62, (distance - preferred) / 32))
            sideways = self.turn * (-1 if int(now / 4.2) % 2 else 1)
            gx, gy = unit(vx * radial - vy * sideways, vy * radial + vx * sideways)
            goal = pos[0] + gx * 14, pos[1] + gy * 14
        elif kind == 'crate':
            mode = 'crate'
            goal = tuple(loot['pos'][:2])
            hold = crate is not None and target is crate and math.dist(pos[:2], goal) < 27
        else:
            mode = 'patrol'
            goal = self.patrol_goal(obs, zone_goal)
            hold = math.dist(pos[:2], goal) < 2

        goal_distance = math.dist(pos[:2], goal)
        if hold or goal_distance < 0.9:
            desired = (0.0, 0.0)
        else:
            nx, ny = self.waypoint(obs, goal) if mode != 'engage' else goal
            desired = unit(nx - pos[0], ny - pos[1])
        running = ((urgent or retreating and not hold or mode == 'engage' and nearest < 30)
                   and me['energy'] > (12 if self.boosting or not me['in_zone'] else 46))
        if mode in ('cube', 'weapon', 'crate', 'drop') and nearest > 85 and goal_distance > 28:
            running = me['energy'] > (48 if self.boosting else 80)
        if mode == 'edge':
            running = False
        move = self.steer(obs, desired, running, enemies, hold, mode == 'edge')
        if goal_distance < 5 and mode in ('drop', 'cube', 'weapon') and not hold:
            scale = min(1.0, max(0.22, goal_distance / 4.8))
            move = (move[0] * scale, move[1] * scale)

        self.motion.append((now, pos[0], pos[1]))
        while self.motion and self.motion[0][0] < now - 0.8:
            self.motion.popleft()
        oldest = self.motion[0]
        stuck = (not hold and goal_distance > 11 and mode != 'engage' and now - oldest[0] > 0.58
                 and math.dist((oldest[1], oldest[2]), pos[:2]) < 1.3)
        jump = False
        if stuck and me['grounded'] and me['energy'] > 34 and now >= self.recover_until:
            self.recover_until = now + 1.55
            self.route_until = 0.0
            jump = True
        if now < self.recover_until and (jump or not me['grounded']):
            running = me['energy'] > 14
            if move == (0.0, 0.0):
                move = desired
        elif not me['grounded']:
            running = False
        self.boosting, self.last_move = running, move
        action = {'tick': obs['tick'], 'move': list(move), 'boost': bool(running and (move != (0.0, 0.0) or jump)),
                  'jump': jump, 'fire': [False, False]}
        self.loot_action(obs, action)
        if target is not None:
            self.fire_control(obs, action, target, slot, solution)
        else:
            self.target_id = None
            if enemies and (retreating or nearest < 85):
                threat = enemies[0]['pos']
                yaw = math.atan2(threat[1] - pos[1], threat[0] - pos[0])
            elif self.damage and self.damage[-1][2] is not None:
                yaw = self.damage[-1][2]
            elif (mode == 'patrol' and not enemies and not hold
                  and (None in me['weapons'] or me['cubes'] < 3)
                  and not any(s['dist'] == 'near' for s in obs['sounds'])):
                # Legs keep following the route as the torso surveys the remaining field of view.
                interval = self.tick_seconds * self.config['match']['decide_every']
                yaw = me['aim'][0] + self.turn * stats['turn_rate'] * interval * 0.85
            elif retreating or hold:
                yaw = me['aim'][0]
            else:
                yaw = math.atan2(goal[1] - pos[1], goal[0] - pos[0]) + 0.62 * math.sin(now * 0.85)
            action['aim'] = [yaw, 0.0]
            action['reload'] = any(w and w['kind'] == 'gun' and 0 < w.get('ammo', 0) <= 6 and not w.get('reload')
                                   for w in me['weapons'])
        if 'drop' in action:
            action['fire'][action['drop']] = False
        if obs['tick'] % 20 == 0:
            mf.draw.label(f'{mode} | {me["hp"]:.0f} HP | {me["cubes"]} cubes')
        return action


if __name__ == '__main__':
    mf.run(Bot)
