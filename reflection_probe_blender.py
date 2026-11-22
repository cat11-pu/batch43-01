class ProbeError(ValueError):
    """Raised when a reflection probe operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReflectionProbeBlender:
    """Reflection probe capture state, normalized blending and parallax.

    Parallax convention: a probe yaw rotation of ``deg`` degrees transforms a
    world offset ``d`` into local space as ``(-dz, dy, dx)`` for 90 degrees and
    ``(dx, dy, dz)`` for 0 degrees, using a right-handed Y rotation.
    """

    def __init__(self, max_probes=8):
        self.max_probes = max_probes

    def add_probe(self, probe_id, position=(0, 0, 0), rotation=(0, 0, 0),
                  box_size=(1, 1, 1), resolution=128, indoor=False):
        raise NotImplementedError

    def update_transform(self, probe_id, position=None, rotation=None):
        raise NotImplementedError

    def remove_probe(self, probe_id):
        raise NotImplementedError

    def capture(self, probe_id, content, cull_characters=True, version=None):
        raise NotImplementedError

    def begin_capture(self, probe_id, now=0):
        raise NotImplementedError

    def finish_capture(self, token, content):
        raise NotImplementedError

    def cancel_capture(self, token):
        raise NotImplementedError

    def capture_state(self, probe_id):
        raise NotImplementedError

    def sample(self, position, camera=None):
        raise NotImplementedError

    def device_lost(self):
        raise NotImplementedError

    def device_restored(self):
        raise NotImplementedError

    def device_state(self):
        raise NotImplementedError

    def callbacks(self):
        raise NotImplementedError
