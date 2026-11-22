class BlendshapeError(ValueError):
    """Raised when a blendshape weight operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BlendshapeMixState:
    """Normalized blendshape weights, frame ordering and LOD channel mapping."""

    def __init__(self, channels=None, lod_map=None):
        self.channels = channels or {}
        self.lod_map = lod_map or {}

    def set(self, character, channel, weight, frame=0):
        raise NotImplementedError

    def mix(self, character, weights=None, frame=0):
        raise NotImplementedError

    def apply(self, character, frame=0):
        raise NotImplementedError

    def reset(self, character):
        raise NotImplementedError

    def set_lod(self, character, lod):
        raise NotImplementedError

    def weights(self, character):
        raise NotImplementedError

    def events(self, character):
        raise NotImplementedError

    def emit_event(self, character, name, frame=0):
        raise NotImplementedError

    def reload_channels(self, channels, lod_map=None):
        raise NotImplementedError

    def device_lost(self):
        raise NotImplementedError

    def device_restored(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
