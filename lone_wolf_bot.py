"""Lone Wolf Labs bot v2 with 3D LOS diagnostics (mechfight/1).

Changes vs v1 (scored 1.38):
- Lead time uses the REAL projectile speed from self.weapons[i].stats.speed
  (v1 hard-coded 220 m/s; the gun is 252 m/s by default -> v1 over-led ~13%).
- Aims at centre of mass (z + 4.5), scaled up 5% per cube the TARGET holds
  (v1 aimed at z + 3.8: shins).
- Own muzzle height read from self.stats.muzzle_height (v1 hard-coded 7.0;
  it grows 5% per cube you collect).
- Fire-gate: the gun only fires when the torso's ACTUAL aim is aligned with
  the desired yaw (shots leave along actual aim, not commanded aim).
- Strafe direction randomised from hello.seed (v1 flipped on a public
  tick//30 pattern expert bots can exploit).
- Target selection: prefers low-HP enemies (they die faster -> placement
  points + cubes) with a distance tiebreak, instead of always nearest.
"""
from __future__ import annotations

import json
import math
import os
import random
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _dist2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class BuildingLOS:
    """3D line-of-sight checks against solid map prisms in /arena/v2/world.json."""

    def __init__(self, map_dir: str) -> None:
        self.cell_size = 24.0
        self.prisms: List[Dict[str, Any]] = []
        self.grid: Dict[Tuple[int, int], List[int]] = {}
        path = os.path.join(map_dir, "v2", "world.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                world = json.load(f)
        except (OSError, ValueError):
            return

        for prism in world.get("prisms", []):
            ring = prism.get("ring", [])
            if len(ring) < 6:
                continue
            xs, ys = ring[0::2], ring[1::2]
            item = {
                "ring": ring,
                "holes": prism.get("holes", []),
                "base": float(prism.get("base", -1e6)),
                "top": float(prism.get("top", 1e6)),
                "bounds": (min(xs), min(ys), max(xs), max(ys)),
            }
            idx = len(self.prisms)
            self.prisms.append(item)
            minx, miny, maxx, maxy = item["bounds"]
            for gx in range(math.floor(minx / self.cell_size), math.floor(maxx / self.cell_size) + 1):
                for gy in range(math.floor(miny / self.cell_size), math.floor(maxy / self.cell_size) + 1):
                    self.grid.setdefault((gx, gy), []).append(idx)

    @staticmethod
    def _inside(x: float, y: float, ring: List[float]) -> bool:
        inside = False
        count = len(ring) // 2
        j = count - 1
        for i in range(count):
            xi, yi = float(ring[2 * i]), float(ring[2 * i + 1])
            xj, yj = float(ring[2 * j]), float(ring[2 * j + 1])
            if (yi > y) != (yj > y):
                crossing_x = (xj - xi) * (y - yi) / (yj - yi) + xi
                if x < crossing_x:
                    inside = not inside
            j = i
        return inside

    def blocked(self, start: List[float], end: List[float]) -> bool:
        """True only if the 3D segment crosses a prism between its base and top."""
        if not self.prisms:
            return False
        dx, dy, dz = end[0] - start[0], end[1] - start[1], end[2] - start[2]
        horizontal = math.hypot(dx, dy)
        steps = max(2, int(math.ceil(horizontal / 1.5)))
        for step in range(1, steps):
            frac = step / steps
            x, y, z = start[0] + dx * frac, start[1] + dy * frac, start[2] + dz * frac
            candidates = self.grid.get((math.floor(x / self.cell_size), math.floor(y / self.cell_size)), ())
            for idx in candidates:
                prism = self.prisms[idx]
                if z < prism["base"] - 0.08 or z > prism["top"] + 0.08:
                    continue
                minx, miny, maxx, maxy = prism["bounds"]
                if x < minx or x > maxx or y < miny or y > maxy:
                    continue
                if not self._inside(x, y, prism["ring"]):
                    continue
                if any(self._inside(x, y, hole) for hole in prism["holes"]):
                    continue
                return True
        return False


class Bot:
    name = "lone-wolf-v2-3d-diag"

    def __init__(self, hello: mf.Hello) -> None:
        cfg = hello.get("config", {})
        map_dir = str(hello.get("map", {}).get("dir", "/arena"))
        self.building_los = BuildingLOS(map_dir)
        loot_cfg = cfg.get("loot", {})
        self.pickup_radius: float = float(loot_cfg.get("pickup_radius", 4.0))
        self.upgrades_cfg: List[Dict[str, Any]] = cfg.get("upgrades", [])
        self.upgrade_priority: Dict[str, int] = {}
        for u in self.upgrades_cfg:
            uid = str(u.get("id", ""))
            effects = u.get("effects", [])
            score = 0
            for ef in effects:
                path = str(ef.get("path", ""))
                if "armor" in uid or "rear" in path or "max_hp" in path or "regen" in path:
                    score += 30
                if "sensor" in uid or "vision" in path or "sound" in path:
                    score += 22
                if "speed" in path or "boost" in path or "energy" in path:
                    score += 18
                if "damage" in path or "rate" in path or "cooldown" in path:
                    score += 12
            self.upgrade_priority[uid] = score
        # CHANGED: seeded RNG from hello.seed so behaviour is reproducible
        # (protocol suggests exactly this) and NOT predictable to opponents.
        self.rng = random.Random(int(hello.get("seed", 1234567)))
        self.last_damage_t: float = -999.0
        self.last_damage_bearing: Optional[float] = None
        self.laser_charging: List[bool] = [False, False]

    def _pick_upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])
        if not choices:
            return 0
        best_idx = 0
        best_score = -10**9
        for idx, ch in enumerate(choices):
            uid = str(ch.get("id", ""))
            desc = str(ch.get("desc", "")).lower()
            s = self.upgrade_priority.get(uid, 0)
            if "armor" in desc or "hp" in desc or "regen" in desc:
                s += 25
            if "vision" in desc or "sensor" in desc or "sound" in desc:
                s += 15
            if "speed" in desc or "energy" in desc:
                s += 12
            if s > best_score:
                best_score = s
                best_idx = idx
        return best_idx

    def _choose_zone_target(self, obs: mf.Observation, x: float, y: float) -> Tuple[float, float]:
        zone = obs["zone"]
        t = float(obs["t"])
        shrink_start = float(zone.get("shrink_start", t))
        shrink_end = float(zone.get("shrink_end", t + 1.0))
        cur_c = zone.get("center", [0.0, 0.0])
        cur_r = float(zone.get("radius", 500.0))
        nxt_c = zone.get("next_center", cur_c)
        nxt_r = float(zone.get("next_radius", cur_r))

        if t >= shrink_start:
            if shrink_end > shrink_start:
                frac = max(0.0, min(1.0, (t - shrink_start) / (shrink_end - shrink_start)))
            else:
                frac = 1.0
            cx = cur_c[0] + (nxt_c[0] - cur_c[0]) * frac
            cy = cur_c[1] + (nxt_c[1] - cur_c[1]) * frac
            rad = cur_r + (nxt_r - cur_r) * frac
        else:
            cx, cy, rad = cur_c[0], cur_c[1], cur_r

        safe_r = max(5.0, rad * 0.72)
        dx_c = nxt_c[0] - cx
        dy_c = nxt_c[1] - cy
        dist_c = math.hypot(dx_c, dy_c)
        if dist_c > 1e-6:
            tx = cx + (dx_c / dist_c) * min(dist_c * 0.65, safe_r * 0.5)
            ty = cy + (dy_c / dist_c) * min(dist_c * 0.65, safe_r * 0.5)
        else:
            tx, ty = cx, cy

        d_me_cur = math.hypot(x - cx, y - cy)
        if d_me_cur < safe_r and math.hypot(x - nxt_c[0], y - nxt_c[1]) > nxt_r * 0.8:
            return float(nxt_c[0]), float(nxt_c[1])
        return float(tx), float(ty)

    def _weapon_fire_decision(
        self,
        slot_idx: int,
        w: Optional[mf.WeaponObs],
        dist: float,
        has_target: bool,
        energy: float,
        target_has_mortar: bool,
    ) -> bool:
        if w is None or not has_target:
            self.laser_charging[slot_idx] = False
            return False

        kind = w.get("kind")
        stats = w.get("stats", {})
        max_range = float(stats.get("range", 200.0))

        if kind == "gun":
            ammo = float(w.get("ammo", 0.0))
            reload_left = float(w.get("reload", 0.0))
            if reload_left > 0.0 or ammo <= 0.0:
                return False
            return dist <= max_range * 0.95

        if kind == "lightning":
            if energy < 12.0 or dist > min(45.0, max_range):
                return False
            return float(w.get("cooldown", 0.0)) <= 0.05

        if kind == "mortar":
            if dist < 24.0 or dist > max_range:
                return False
            return float(w.get("cooldown", 0.0)) <= 0.05

        if kind == "laser":
            charge_time = float(w.get("charge", 0.0))
            full_charge = float(stats.get("charge", 0.75))
            if dist > max_range:
                self.laser_charging[slot_idx] = False
                return False
            target_charge = full_charge * (0.85 if target_has_mortar else 0.98)
            if not self.laser_charging[slot_idx]:
                if float(w.get("cooldown", 0.0)) <= 0.05 and energy > 15.0:
                    self.laser_charging[slot_idx] = True
                    return True
                return False
            if charge_time >= target_charge:
                self.laser_charging[slot_idx] = False
                return False
            return True

        return False

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))
        t = float(obs["t"])
        my_stats = me.get("stats", {})
        # CHANGED: real muzzle height (grows with cubes); default 7.0 at spawn.
        muzzle_z = z + float(my_stats.get("muzzle_height", 7.0))

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damage_t = t
                if ev.get("bearing") is not None:
                    self.last_damage_bearing = float(ev["bearing"])

        act: mf.Action = {"tick": obs["tick"]}

        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])  # type: ignore[arg-type]

        weapons = me.get("weapons", [None, None])
        gun_slot = weapons[0] if len(weapons) > 0 and weapons[0] and weapons[0].get("kind") == "gun" else None
        # CHANGED: real bullet speed for lead (upgrades change it mid-match).
        bullet_speed = 252.0
        if gun_slot is not None:
            bullet_speed = max(1.0, float(gun_slot.get("stats", {}).get("speed", 252.0)))
            ammo = float(gun_slot.get("ammo", 0.0))
            mag = float(gun_slot.get("stats", {}).get("mag", 12.0))
            reload_left = float(gun_slot.get("reload", 0.0))
            if reload_left <= 0.0 and ammo <= max(2.0, mag * 0.25):
                act["reload"] = True

        pickups = obs.get("pickups", [])
        if len(weapons) > 1 and weapons[1] is None:
            for p in pickups:
                pkind = p.get("kind")
                if pkind in ("laser", "lightning", "mortar") and _dist2d(p["pos"], [x, y]) <= self.pickup_radius:
                    act["take"] = p["id"]
                    break

        mechs = obs.get("mechs", [])
        hostile_mechs = [m for m in mechs if m.get("kind") == "mech"]
        # CHANGED: target selection scores by threat-free weakness:
        # weakest-first gives kills + cubes; distance breaks ties. House bots
        # hunt the weakest too, so killing the weak one first also removes the
        # squad's own preferred target.
        if hostile_mechs:
            def tgt_score(m: Dict[str, Any]) -> float:
                d = math.dist(m["pos"], [x, y, z])
                hp_r = float(m.get("hp", 1400.0)) / max(1.0, float(m.get("max_hp", 1400.0)))
                return d + hp_r * 90.0
            hostile_mechs.sort(key=tgt_score)
        else:
            hostile_mechs.sort(key=lambda m: math.dist(m["pos"], [x, y, z]))

        recently_hit = (t - self.last_damage_t) < 4.5
        low_hp = hp_ratio < 0.45

        zx, zy = self._choose_zone_target(obs, x, y)
        move_x = zx - x
        move_y = zy - y

        nearest_enemy = hostile_mechs[0] if hostile_mechs else None
        desired_yaw: Optional[float] = None
        if nearest_enemy is not None and not low_hp:
            ex, ey, _ = nearest_enemy["pos"]
            edist = math.hypot(ex - x, ey - y)
            to_enemy_yaw = math.atan2(ey - y, ex - x)
            # CHANGED: randomised strafe side + random flip interval instead
            # of the public tick//30 pattern.
            if not hasattr(self, "_strafe_dir") or self.rng.random() < 0.02:
                self._strafe_dir = self.rng.choice([-1.0, 1.0])
                self._strafe_until = t + self.rng.uniform(1.5, 3.5)
            if t > getattr(self, "_strafe_until", 0.0):
                self._strafe_dir *= -1.0
                self._strafe_until = t + self.rng.uniform(1.5, 3.5)
            strafe_yaw = to_enemy_yaw + self._strafe_dir * (math.pi * 0.5)
            desired_range = 75.0 if hp_ratio > 0.6 else 115.0
            radial_pull = 0.0
            if edist < desired_range * 0.75:
                radial_pull = -0.65
            elif edist > desired_range * 1.35 and in_zone:
                radial_pull = 0.35

            blend_x = (zx - x) * (1.2 if not in_zone else 0.45) + math.cos(to_enemy_yaw) * radial_pull * 40.0 + math.cos(strafe_yaw) * 28.0
            blend_y = (zy - y) * (1.2 if not in_zone else 0.45) + math.sin(to_enemy_yaw) * radial_pull * 40.0 + math.sin(strafe_yaw) * 28.0
            move_x, move_y = blend_x, blend_y

        move_norm = math.hypot(move_x, move_y)
        if move_norm > 1e-6:
            speed_scale = 0.38 if (low_hp and not recently_hit and in_zone and len(hostile_mechs) == 0) else 1.0
            act["move"] = [(move_x / move_norm) * speed_scale, (move_y / move_norm) * speed_scale]
        else:
            act["move"] = [0.0, 0.0]

        should_boost = (not in_zone and energy > 20.0) or (recently_hit and low_hp and energy > 35.0)
        act["boost"] = bool(should_boost)
        act["jump"] = bool(me.get("grounded", False) and (self.rng.random() < 0.02) and len(hostile_mechs) > 0)

        los_diagnostic = "n/a"
        if nearest_enemy is not None:
            tpos = nearest_enemy["pos"]
            tvel = nearest_enemy.get("vel", [0.0, 0.0, 0.0])
            horiz_dist = max(1.0, math.hypot(tpos[0] - x, tpos[1] - y))
            # CHANGED: lead from the real projectile speed, not 220.
            lead_t = min(0.9, horiz_dist / bullet_speed)
            px = tpos[0] + tvel[0] * lead_t
            py = tpos[1] + tvel[1] * lead_t
            # Preserve v2's scored target-height behavior.
            t_cubes = int(nearest_enemy.get("cubes", 0))
            pz = tpos[2] + 4.5 * (1.0 + 0.05 * min(t_cubes, 10))
            # Test the actual direct-fire aim ray, including the horizontal lead.
            shot_point = [float(px), float(py), float(pz)]
            blocked_by_building = self.building_los.blocked([x, y, muzzle_z], shot_point)
            los_diagnostic = "blocked" if blocked_by_building else "clear"
            yaw = math.atan2(py - y, px - x)
            pitch = math.atan2(pz - muzzle_z, horiz_dist)
            pitch = max(-0.8, min(0.8, pitch))
            act["aim"] = [yaw, pitch]
            desired_yaw = yaw

            target_weapons = nearest_enemy.get("weapons", [])
            target_has_mortar = "mortar" in target_weapons
            f0 = self._weapon_fire_decision(0, weapons[0] if len(weapons) > 0 else None, horiz_dist, True, energy, target_has_mortar)
            f1 = self._weapon_fire_decision(1, weapons[1] if len(weapons) > 1 else None, horiz_dist, True, energy, target_has_mortar)
            fire = [f0, f1]
            # CHANGED: fire-gate. Shots leave along self.aim (actual torso
            # aim), which slews at turn_rate. Hold trigger only when aligned;
            # exception: keep holding a charging laser (aim slews fine while
            # charging, charge_turn_mult is 1).
            my_aim = me.get("aim", [yaw, pitch])
            aim_err = abs(_wrap_angle(float(my_aim[0]) - yaw))
            for i, w in enumerate(weapons[:2]):
                if w is not None and w.get("kind") == "laser" and self.laser_charging[i]:
                    continue
                if aim_err > 0.05:
                    fire[i] = False
            act["fire"] = fire
            mf.draw.line([x, y, muzzle_z], [px, py, pz], "#ff5040")
        else:
            self.laser_charging = [False, False]
            if recently_hit and self.last_damage_bearing is not None:
                act["aim"] = [_wrap_angle(self.last_damage_bearing), 0.0]
            else:
                act["aim"] = [math.atan2(zy - y, zx - x), 0.0]
            act["fire"] = [False, False]

        mf.draw.point([zx, zy, z], "#40c0ff", 6)
        mf.draw.label(f"hp {hp:.0f}/{max_hp:.0f} · z{'1' if in_zone else '0'} · e {len(hostile_mechs)} · 3D LOS {los_diagnostic}")
        return act


mf.run(Bot)

