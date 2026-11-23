import math


class WaterError(ValueError):
    """Raised when a water surface parameter or LOD request is invalid."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class WaterSurfaceCompositor:
    """Single-source wave height, LOD stitching and surface compositing."""

    _QUALITY_AMPLITUDE = {"low": 0.5, "medium": 0.75, "high": 1.0}
    _QUALITY_RESOLUTION = {"low": 32, "medium": 48, "high": 64}
    _SHALLOW_COLOR = (0.2, 0.6, 0.6)
    _DEEP_COLOR = (0.0, 0.1, 0.4)

    def __init__(self, quality="high"):
        self._wakes = {}
        self._shader = "ok"
        self.set_quality(quality)

    def set_quality(self, quality):
        if quality not in self._QUALITY_AMPLITUDE:
            raise WaterError("invalid_quality", quality)
        self.quality = quality
        self._amplitude_scale = self._QUALITY_AMPLITUDE[quality]
        self._resolution = self._QUALITY_RESOLUTION[quality]

    def _base_wave(self, x, y, t):
        return (
            0.30 * math.sin(0.8 * x + 1.3 * t)
            + 0.20 * math.sin(1.1 * y - 0.9 * t)
            + 0.10 * math.sin(0.6 * (x + y) + 2.1 * t)
        )

    def wave_height(self, x, y, t):
        return self._amplitude_scale * self._base_wave(x, y, t)

    def render_height(self, x, y, t):
        return self.wave_height(x, y, t)

    def mesh_resolution(self):
        return self._resolution

    def edge_vertices(self, lod):
        if lod not in self._QUALITY_RESOLUTION:
            raise WaterError("invalid_lod", lod)
        resolution = self._QUALITY_RESOLUTION[lod]
        return [index / resolution for index in range(resolution + 1)]

    def seam(self, lod_a, lod_b):
        edge_a = self.edge_vertices(lod_a)
        edge_b = self.edge_vertices(lod_b)
        shared = set(edge_a) & set(edge_b)
        return sorted(shared)

    def blend_color(self, depth, max_depth=10.0):
        if max_depth <= 0.0:
            raise WaterError("invalid_max_depth", max_depth)
        factor = min(max(depth / max_depth, 0.0), 1.0)
        return tuple(
            shallow + (deep - shallow) * factor
            for shallow, deep in zip(self._SHALLOW_COLOR, self._DEEP_COLOR)
        )

    def foam(self, x, y, t, slope=0.0):
        width = 1.0 / (1.0 + max(slope, 0.0))
        return {"width": width, "height": self.wave_height(x, y, t)}

    def refract(self, surface_depth, scene_depth):
        return {"clipped": scene_depth < surface_depth}

    def sort_order(self, items):
        def key(item):
            if item.get("opaque", False):
                return (0, 0.0)
            return (1, -item.get("distance", 0.0))

        return [item["id"] for item in sorted(items, key=key)]

    def specular(self, x, y, t, sun=(0.0, 0.0, 1.0)):
        epsilon = 1e-3
        base = self._base_wave(x, y, t)
        dx = (self._base_wave(x + epsilon, y, t) - base) / epsilon
        dy = (self._base_wave(x, y + epsilon, t) - base) / epsilon
        normal = _normalize((-dx, -dy, 1.0))
        light = _normalize(sun)
        half = _normalize((light[0], light[1], light[2] + 1.0))
        cosine = max(
            normal[0] * half[0] + normal[1] * half[1] + normal[2] * half[2],
            0.0,
        )
        return cosine ** 64

    def add_wake(self, wake_id, x, y, t, ttl=10.0):
        if ttl <= 0.0:
            raise WaterError("invalid_wake_ttl", ttl)
        self._wakes[wake_id] = {"x": x, "y": y, "t": t, "ttl": ttl}
        self._prune_wakes(t)

    def active_wakes(self, t):
        self._prune_wakes(t)
        return [dict(wake, id=wake_id) for wake_id, wake in self._wakes.items()]

    def _prune_wakes(self, t):
        expired = [
            wake_id
            for wake_id, wake in self._wakes.items()
            if t - wake["t"] >= wake["ttl"]
        ]
        for wake_id in expired:
            del self._wakes[wake_id]

    def surface_mode(self, camera_height, x, y, t):
        surface = self.wave_height(x, y, t)
        return "above" if camera_height >= surface else "underwater"

    def visible(self, camera_height, near=0.1):
        return True

    def shader_state(self):
        return self._shader

    def device_lost(self):
        self._shader = "lost"

    def device_restored(self):
        self._shader = "ok"


def _normalize(vector):
    length = math.sqrt(sum(component * component for component in vector))
    if length == 0.0:
        return tuple(0.0 for _ in vector)
    return tuple(component / length for component in vector)
