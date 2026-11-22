class BloomError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BloomChainThreshold:
    """Exposure-aware bloom threshold, downsample chain and composite."""

    def __init__(self, levels=5, threshold=1.0, strength=1.0):
        self.levels = levels
        self.threshold = threshold
        self.strength = strength

    def configure(self, exposure=1.0, resolution=(64, 64), levels=None):
        raise NotImplementedError

    def extract(self, luminance, exposure=None):
        raise NotImplementedError

    def downsample(self, level, image):
        raise NotImplementedError

    def composite(self, levels, weights=None):
        raise NotImplementedError

    def render(self, scene, ui=None, particles=None, chain_version=None):
        raise NotImplementedError

    def set_enabled(self, enabled):
        raise NotImplementedError

    def set_exposure(self, exposure):
        raise NotImplementedError

    def set_resolution(self, resolution):
        raise NotImplementedError

    def load_scene(self, scene_id):
        raise NotImplementedError

    def chain_state(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
