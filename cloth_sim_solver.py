import copy
import math


class ClothError(ValueError):
    """Raised when a cloth simulation request is invalid."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ClothSimSolver:
    """Fixed-step cloth simulation with anchors, colliders and LOD state."""

    MAX_FRAME_DT = 0.25

    def __init__(self, fixed_dt=0.01, iterations=8, gravity=-9.8):
        if fixed_dt <= 0:
            raise ClothError("INVALID_FIXED_DT", fixed_dt)
        if iterations < 0:
            raise ClothError("INVALID_ITERATIONS", iterations)
        self.fixed_dt = float(fixed_dt)
        self.iterations = int(iterations)
        self.gravity = float(gravity)
        self._cloths = {}
        self._colliders = {}
        self._device_valid = True

    def _require(self, cloth_id):
        cloth = self._cloths.get(cloth_id)
        if cloth is None:
            raise ClothError("UNKNOWN_CLOTH", cloth_id)
        return cloth

    @staticmethod
    def _vec3(value):
        return (float(value[0]), float(value[1]), float(value[2]))

    @staticmethod
    def _add(a, b):
        return (a[0] + b[0], a[1] + b[1], a[2] + b[2])

    @staticmethod
    def _scale(a, factor):
        return (a[0] * factor, a[1] * factor, a[2] * factor)

    @staticmethod
    def _sub(a, b):
        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    @staticmethod
    def _length(a):
        return (a[0] * a[0] + a[1] * a[1] + a[2] * a[2]) ** 0.5

    def create_cloth(self, cloth_id, particles, anchors=None, constraints=None):
        if cloth_id in self._cloths:
            raise ClothError("DUPLICATE_CLOTH", cloth_id)
        initial = [self._vec3(position) for position in particles]
        if not initial:
            raise ClothError("EMPTY_CLOTH", cloth_id)
        anchor_targets = {}
        for index, bone_position in (anchors or {}).items():
            index = int(index)
            if not 0 <= index < len(initial):
                raise ClothError("INVALID_ANCHOR", index)
            anchor_targets[index] = self._vec3(bone_position)
        if constraints is None:
            constraint_list = []
            for index in range(len(initial) - 1):
                rest = self._length(self._sub(initial[index + 1],
                                               initial[index]))
                constraint_list.append((index, index + 1, rest, 1.0))
        else:
            constraint_list = []
            for constraint in constraints:
                first = int(constraint[0])
                second = int(constraint[1])
                if not (0 <= first < len(initial)
                        and 0 <= second < len(initial)
                        and first != second):
                    raise ClothError("INVALID_CONSTRAINT", constraint)
                rest = (float(constraint[2]) if len(constraint) > 2
                        else self._length(self._sub(initial[second],
                                                    initial[first])))
                stiffness = float(constraint[3]) if len(constraint) > 3 else 1.0
                constraint_list.append((first, second, rest, stiffness))
        positions = [dict(anchor_targets).get(index, position)
                     for index, position in enumerate(initial)]
        self._cloths[cloth_id] = {
            "initial": initial,
            "positions": positions,
            "previous": list(positions),
            "anchors": anchor_targets,
            "constraints": constraint_list,
            "wind": (0.0, 0.0, 0.0),
            "yaw": 0.0,
            "lod": "high",
            "accumulator": 0.0,
            "substeps": 0,
        }

    def _wind_local(self, cloth):
        yaw = cloth["yaw"] * 3.141592653589793 / 180.0
        cosine = math.cos(yaw)
        sine = math.sin(yaw)
        wind = cloth["wind"]
        return (cosine * wind[0] - sine * wind[2],
                wind[1],
                sine * wind[0] + cosine * wind[2])

    def _integrate(self, cloth):
        dt2 = self.fixed_dt * self.fixed_dt
        wind = self._wind_local(cloth)
        positions = cloth["positions"]
        previous = cloth["previous"]
        next_positions = []
        for index, position in enumerate(positions):
            if index in cloth["anchors"]:
                next_positions.append(position)
                continue
            acceleration = (wind[0], self.gravity + wind[1], wind[2])
            next_positions.append(tuple(
                position[axis]
                + (position[axis] - previous[index][axis])
                + acceleration[axis] * dt2
                for axis in range(3)))
        cloth["previous"] = positions
        cloth["positions"] = next_positions

    def _solve_anchors(self, cloth):
        positions = cloth["positions"]
        previous = cloth["previous"]
        for index, target in cloth["anchors"].items():
            positions[index] = target
            previous[index] = target

    def _solve_constraints(self, cloth):
        positions = cloth["positions"]
        for first, second, rest, stiffness in cloth["constraints"]:
            if first in cloth["anchors"] and second in cloth["anchors"]:
                continue
            delta = self._sub(positions[second], positions[first])
            distance = self._length(delta)
            if distance <= 1e-12:
                continue
            correction = (distance - rest) / distance * stiffness
            offset = self._scale(delta, correction)
            if first in cloth["anchors"]:
                positions[second] = self._sub(positions[second], offset)
            elif second in cloth["anchors"]:
                positions[first] = self._add(positions[first], offset)
            else:
                positions[first] = self._add(
                    positions[first], self._scale(offset, 0.5))
                positions[second] = self._sub(
                    positions[second], self._scale(offset, 0.5))

    def _solve_collisions(self, cloth):
        positions = cloth["positions"]
        previous = cloth["previous"]
        for index, position in enumerate(positions):
            if index in cloth["anchors"]:
                continue
            for center, radius in self._colliders.values():
                delta = self._sub(position, center)
                distance = self._length(delta)
                if distance < radius and distance > 1e-12:
                    target = self._add(
                        center, self._scale(delta, radius / distance))
                    push = self._sub(target, position)
                    positions[index] = target
                    previous[index] = self._add(previous[index], push)
                elif distance <= 1e-12:
                    target = (center[0] + radius, center[1], center[2])
                    push = self._sub(target, position)
                    positions[index] = target
                    previous[index] = self._add(previous[index], push)

    def _substep(self, cloth):
        self._integrate(cloth)
        for _ in range(self.iterations):
            self._solve_constraints(cloth)
            self._solve_anchors(cloth)
            self._solve_collisions(cloth)
        self._solve_anchors(cloth)
        self._solve_collisions(cloth)

    def step(self, cloth_id, frame_dt):
        cloth = self._require(cloth_id)
        if frame_dt < 0:
            raise ClothError("INVALID_FRAME_DT", frame_dt)
        if frame_dt == 0 or not self._device_valid:
            return
        cloth["accumulator"] += min(float(frame_dt), self.MAX_FRAME_DT)
        count = int(cloth["accumulator"] / self.fixed_dt + 1e-9)
        for _ in range(count):
            self._substep(cloth)
        cloth["accumulator"] -= count * self.fixed_dt
        cloth["substeps"] += count

    def positions(self, cloth_id):
        return [list(position) for position in self._require(cloth_id)["positions"]]

    def active_indices(self, cloth_id):
        cloth = self._require(cloth_id)
        count = len(cloth["positions"])
        if cloth["lod"] == "low":
            return [index for index in range(count) if index % 2 == 0]
        return list(range(count))

    def stats(self, cloth_id):
        cloth = self._require(cloth_id)
        return {
            "substeps": cloth["substeps"],
            "iterations": self.iterations,
            "accumulator": cloth["accumulator"],
            "lod": cloth["lod"],
            "particle_count": len(cloth["positions"]),
            "device_valid": self._device_valid,
        }

    def set_lod(self, cloth_id, lod):
        cloth = self._require(cloth_id)
        if lod not in ("high", "low"):
            raise ClothError("INVALID_LOD", lod)
        cloth["lod"] = lod

    def set_collider(self, collider_id, center, radius):
        if radius < 0:
            raise ClothError("INVALID_COLLIDER", collider_id)
        self._colliders[collider_id] = (self._vec3(center), float(radius))

    def set_anchor(self, cloth_id, index, bone_position):
        cloth = self._require(cloth_id)
        index = int(index)
        if index not in cloth["anchors"]:
            raise ClothError("UNKNOWN_ANCHOR", index)
        cloth["anchors"][index] = self._vec3(bone_position)

    def set_wind(self, cloth_id, direction_world):
        self._require(cloth_id)["wind"] = self._vec3(direction_world)

    def set_character_rotation(self, cloth_id, yaw_degrees):
        self._require(cloth_id)["yaw"] = float(yaw_degrees)

    def wind_local(self, cloth_id):
        return list(self._wind_local(self._require(cloth_id)))

    def reset(self, cloth_id):
        cloth = self._require(cloth_id)
        positions = [dict(cloth["anchors"]).get(index, position)
                     for index, position in enumerate(cloth["initial"])]
        cloth["positions"] = positions
        cloth["previous"] = list(positions)
        cloth["accumulator"] = 0.0
        cloth["substeps"] = 0

    def device_lost(self):
        self._device_valid = False

    def device_restored(self):
        self._device_valid = True

    def dump(self):
        return {
            "config": {
                "fixed_dt": self.fixed_dt,
                "iterations": self.iterations,
                "gravity": self.gravity,
            },
            "colliders": copy.deepcopy(self._colliders),
            "cloths": copy.deepcopy(self._cloths),
            "device_valid": self._device_valid,
        }

    def load(self, state):
        state = copy.deepcopy(state)
        config = state["config"]
        self.fixed_dt = float(config["fixed_dt"])
        self.iterations = int(config["iterations"])
        self.gravity = float(config["gravity"])
        self._colliders = {
            key: (tuple(center), float(radius))
            for key, (center, radius) in state["colliders"].items()}
        self._cloths = state["cloths"]
        for cloth in self._cloths.values():
            cloth["initial"] = [tuple(position) for position in cloth["initial"]]
            cloth["positions"] = [tuple(position) for position in cloth["positions"]]
            cloth["previous"] = [tuple(position) for position in cloth["previous"]]
            cloth["anchors"] = {int(index): tuple(target)
                                for index, target in cloth["anchors"].items()}
            cloth["wind"] = tuple(cloth["wind"])
            cloth["constraints"] = [tuple(constraint)
                                    for constraint in cloth["constraints"]]
        self._device_valid = bool(state.get("device_valid", True))
