class ClothError(ValueError):
    """Raised when a cloth simulation request is invalid."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ClothSimSolver:
    """Fixed-step cloth simulation with anchors, colliders and LOD state."""

    def __init__(self, fixed_dt=0.01, iterations=8, gravity=-9.8):
        self.fixed_dt = fixed_dt
        self.iterations = iterations
        self.gravity = gravity

    def create_cloth(self, cloth_id, particles, anchors=None, constraints=None):
        raise NotImplementedError

    def step(self, cloth_id, frame_dt):
        raise NotImplementedError

    def positions(self, cloth_id):
        raise NotImplementedError

    def active_indices(self, cloth_id):
        raise NotImplementedError

    def stats(self, cloth_id):
        raise NotImplementedError

    def set_lod(self, cloth_id, lod):
        raise NotImplementedError

    def set_collider(self, collider_id, center, radius):
        raise NotImplementedError

    def set_anchor(self, cloth_id, index, bone_position):
        raise NotImplementedError

    def set_wind(self, cloth_id, direction_world):
        raise NotImplementedError

    def set_character_rotation(self, cloth_id, yaw_degrees):
        raise NotImplementedError

    def wind_local(self, cloth_id):
        raise NotImplementedError

    def reset(self, cloth_id):
        raise NotImplementedError

    def device_lost(self):
        raise NotImplementedError

    def device_restored(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
