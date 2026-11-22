class FoliageError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FoliageInstancingMeadow:
    """Foliage instance buffers, wind phase and LOD selection."""

    def __init__(self, wind_frequency=0.5):
        self.wind_frequency = wind_frequency

    def add_species(self, species, capacity):
        raise NotImplementedError

    def set_terrain_height(self, x, z, height, version=1):
        raise NotImplementedError

    def place(self, species, x, z, count=1, terrain_version=None):
        raise NotImplementedError

    def wind_phase(self, x, z, time):
        raise NotImplementedError

    def wind_strength(self, time):
        raise NotImplementedError

    def density_for(self, distance):
        raise NotImplementedError

    def lod_for(self, distance):
        raise NotImplementedError

    def shadow_lod_for(self, distance):
        raise NotImplementedError

    def visible_instances(self, species, camera_radius=30.0,
                          origin=(0.0, 0.0)):
        raise NotImplementedError

    def rebuild(self, species):
        raise NotImplementedError

    def commit_rebuild(self, species, version):
        raise NotImplementedError

    def buffer_version(self, species):
        raise NotImplementedError

    def instances(self, species):
        raise NotImplementedError

    def stomp(self, species, index):
        raise NotImplementedError

    def fade_alpha(self, x, z, time):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
