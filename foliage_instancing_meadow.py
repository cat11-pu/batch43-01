import copy
import math


class FoliageError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FoliageInstancingMeadow:
    """Foliage instance buffers, wind phase and LOD selection."""

    def __init__(self, wind_frequency=0.5, capacity=100):
        self.wind_frequency = float(wind_frequency)
        self.default_capacity = capacity
        self._species = {}
        self._terrain = {}
        self._terrain_version = 0
        self._next_instance_id = 0
        self._rebuild_serial = 0

    def _require_species(self, species):
        record = self._species.get(species)
        if record is None:
            raise FoliageError("UNKNOWN_SPECIES", species)
        return record

    def add_species(self, species, capacity=None):
        if not isinstance(species, str) or not species:
            raise FoliageError("INVALID_SPECIES", species)
        if species in self._species:
            raise FoliageError("DUPLICATE_SPECIES", species)
        if capacity is None:
            capacity = self.default_capacity
        if (not isinstance(capacity, int) or isinstance(capacity, bool)
                or capacity < 0):
            raise FoliageError("INVALID_CAPACITY", capacity)
        self._species[species] = {
            "capacity": capacity,
            "instances": [],
            "version": 0,
            "pending": None,
        }
        return {"species": species, "capacity": capacity}

    def set_terrain_height(self, x, z, height, version=1):
        key = (float(x), float(z))
        self._terrain[key] = {
            "height": float(height),
            "version": int(version),
        }
        self._terrain_version = int(version)
        return {
            "x": key[0],
            "z": key[1],
            "height": float(height),
            "version": int(version),
        }

    def _terrain_sample(self, x, z, requested_version):
        key = (float(x), float(z))
        sample = self._terrain.get(key)
        if requested_version is not None:
            requested_version = int(requested_version)
            current_version = (
                sample["version"] if sample is not None
                else self._terrain_version
            )
            if requested_version != current_version:
                raise FoliageError(
                    "STALE_TERRAIN",
                    {
                        "requested": requested_version,
                        "current": current_version,
                    },
                )
        if sample is None:
            return 0.0, None
        return sample["height"], sample["version"]

    def place(self, species, x, z, count=1, terrain_version=None):
        record = self._require_species(species)
        if (not isinstance(count, int) or isinstance(count, bool)
                or count <= 0):
            raise FoliageError("INVALID_COUNT", count)
        if len(record["instances"]) + count > record["capacity"]:
            raise FoliageError(
                "CAPACITY_EXCEEDED",
                {
                    "species": species,
                    "capacity": record["capacity"],
                    "live": len(record["instances"]),
                    "requested": count,
                },
            )

        height, sample_version = self._terrain_sample(
            x, z, terrain_version
        )
        placed = []
        for _ in range(count):
            instance = {
                "id": self._next_instance_id,
                "species": species,
                "x": float(x),
                "y": height,
                "z": float(z),
                "terrain_version": sample_version,
                "stomped": False,
                "radius": 0.5,
            }
            self._next_instance_id += 1
            record["instances"].append(instance)
            placed.append(copy.deepcopy(instance))
        return {"species": species, "placed": count, "instances": placed}

    def wind_phase(self, x, z, time):
        phase = (
            float(x) * 1.618033988749895
            + float(z) * 2.414213562373095
            + float(time) * self.wind_frequency * math.tau
        )
        return phase % math.tau

    def wind_strength(self, time):
        wave = math.sin(float(time) * self.wind_frequency * math.tau)
        return 0.5 + 0.5 * wave

    def density_for(self, distance):
        distance = abs(float(distance))
        if math.isinf(distance):
            return 0.0
        return 1.0 / (1.0 + (distance / 50.0) ** 2)

    def lod_for(self, distance):
        distance = abs(float(distance))
        if distance <= 20.0:
            return 0
        if distance <= 50.0:
            return 1
        return 2

    def shadow_lod_for(self, distance):
        return self.lod_for(distance)

    def visible_instances(self, species, camera_radius=30.0,
                          origin=(0.0, 0.0)):
        record = self._require_species(species)
        origin_x, origin_z = float(origin[0]), float(origin[1])
        radius = float(camera_radius)
        visible = []
        for instance in record["instances"]:
            dx = instance["x"] - origin_x
            dz = instance["z"] - origin_z
            cull_radius = radius + float(instance.get("radius", 0.0))
            if dx * dx + dz * dz <= cull_radius * cull_radius:
                visible.append(copy.deepcopy(instance))
        return visible

    def rebuild(self, species):
        record = self._require_species(species)
        self._rebuild_serial += 1
        version = self._rebuild_serial
        staged = copy.deepcopy(record["instances"])
        record["pending"] = {
            "version": version,
            "instances": staged,
        }
        return {
            "species": species,
            "version": version,
            "count": len(staged),
        }

    def commit_rebuild(self, species, version):
        record = self._require_species(species)
        pending = record["pending"]
        if pending is None or pending["version"] != int(version):
            raise FoliageError(
                "STALE_REBUILD",
                {
                    "requested": int(version),
                    "pending": None if pending is None
                    else pending["version"],
                },
            )
        record["instances"] = pending["instances"]
        record["version"] = pending["version"]
        record["pending"] = None
        return {
            "committed": True,
            "species": species,
            "version": record["version"],
            "count": len(record["instances"]),
        }

    def buffer_version(self, species):
        return self._require_species(species)["version"]

    def instances(self, species):
        return copy.deepcopy(self._require_species(species)["instances"])

    def stomp(self, species, index):
        record = self._require_species(species)
        if (not isinstance(index, int) or isinstance(index, bool)
                or index < 0 or index >= len(record["instances"])):
            raise FoliageError("INVALID_INSTANCE", index)
        instance = record["instances"][index]
        instance["stomped"] = True
        instance_id = instance["id"]
        pending = record["pending"]
        if pending is not None:
            for staged in pending["instances"]:
                if staged["id"] == instance_id:
                    staged["stomped"] = True
                    break
        return copy.deepcopy(instance)

    def fade_alpha(self, x, z, time):
        phase = self.wind_phase(x, z, time)
        return 0.5 + 0.5 * math.sin(phase)

    def dump(self):
        terrain = []
        for (x, z), sample in self._terrain.items():
            terrain.append({
                "x": x,
                "z": z,
                "height": sample["height"],
                "version": sample["version"],
            })
        return {
            "wind_frequency": self.wind_frequency,
            "default_capacity": self.default_capacity,
            "terrain_version": self._terrain_version,
            "terrain": terrain,
            "species": copy.deepcopy(self._species),
            "next_instance_id": self._next_instance_id,
            "rebuild_serial": self._rebuild_serial,
        }

    def load(self, state):
        self.wind_frequency = float(
            state.get("wind_frequency", self.wind_frequency)
        )
        self.default_capacity = state.get(
            "default_capacity", self.default_capacity
        )
        self._terrain = {}
        for sample in state.get("terrain", []):
            key = (float(sample["x"]), float(sample["z"]))
            self._terrain[key] = {
                "height": float(sample["height"]),
                "version": int(sample["version"]),
            }
        self._terrain_version = int(
            state.get("terrain_version", self._terrain_version)
        )
        self._species = copy.deepcopy(state.get("species", {}))
        for record in self._species.values():
            record.setdefault("capacity", self.default_capacity)
            record.setdefault("instances", [])
            record.setdefault("version", 0)
            record.setdefault("pending", None)
        self._next_instance_id = int(
            state.get("next_instance_id", self._next_instance_id)
        )
        self._rebuild_serial = int(
            state.get("rebuild_serial", self._rebuild_serial)
        )
        return self
