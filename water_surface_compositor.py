class WaterError(ValueError):
    """Raised when a water surface parameter or LOD request is invalid."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class WaterSurfaceCompositor:
    """Single-source wave height, LOD stitching and surface compositing."""

    def __init__(self, quality="high"):
        self.quality = quality

    def set_quality(self, quality):
        raise NotImplementedError

    def wave_height(self, x, y, t):
        raise NotImplementedError

    def render_height(self, x, y, t):
        raise NotImplementedError

    def mesh_resolution(self):
        raise NotImplementedError

    def edge_vertices(self, lod):
        raise NotImplementedError

    def seam(self, lod_a, lod_b):
        raise NotImplementedError

    def blend_color(self, depth, max_depth=10.0):
        raise NotImplementedError

    def foam(self, x, y, t, slope=0.0):
        raise NotImplementedError

    def refract(self, surface_depth, scene_depth):
        raise NotImplementedError

    def sort_order(self, items):
        raise NotImplementedError

    def specular(self, x, y, t, sun=(0.0, 0.0, 1.0)):
        raise NotImplementedError

    def add_wake(self, wake_id, x, y, t, ttl=10.0):
        raise NotImplementedError

    def active_wakes(self, t):
        raise NotImplementedError

    def surface_mode(self, camera_height, x, y, t):
        raise NotImplementedError

    def visible(self, camera_height, near=0.1):
        raise NotImplementedError

    def shader_state(self):
        raise NotImplementedError

    def device_lost(self):
        raise NotImplementedError

    def device_restored(self):
        raise NotImplementedError
