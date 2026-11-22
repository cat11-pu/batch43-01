class LutError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ColorLutGrader:
    """Color LUT sampling, strength blending and hot reload."""

    def __init__(self, strength=1.0, exposure=1.0):
        self.strength = strength
        self.exposure = exposure

    def load_lut(self, entries, size, domain=(0.0, 1.0), version=1,
                 flip=False):
        raise NotImplementedError

    def sample(self, rgb):
        raise NotImplementedError

    def grade(self, rgb, lut_version=None):
        raise NotImplementedError

    def set_strength(self, strength):
        raise NotImplementedError

    def set_exposure(self, exposure):
        raise NotImplementedError

    def reload(self, entries, size, version, flip=False):
        raise NotImplementedError

    def grade_backend(self, rgb, backend="d3d12"):
        raise NotImplementedError

    def render(self, colors, ui_colors=None):
        raise NotImplementedError

    def lut_state(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
