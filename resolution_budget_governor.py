class GovernorError(ValueError):
    """Raised when a resolution budget request is invalid."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ResolutionBudgetGovernor:
    """Hysteresis based dynamic resolution with native UI and screenshots."""

    def __init__(self, scales=(0.5, 0.7, 1.0), target_fps=60,
                 native=(1920, 1080)):
        self.scales = list(scales)
        self.target_fps = target_fps
        self.native = tuple(native)

    def sample(self, frame_time_ms=None, gpu_ms=None, cpu_ms=None,
               view="main"):
        raise NotImplementedError

    def decide(self):
        raise NotImplementedError

    def apply(self, scale):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError

    def scale(self):
        raise NotImplementedError

    def ui_size(self):
        raise NotImplementedError

    def render_size(self):
        raise NotImplementedError

    def capture(self, kind="screenshot"):
        raise NotImplementedError

    def buffers(self):
        raise NotImplementedError

    def register_buffer(self, name, width, height):
        raise NotImplementedError

    def late_callback(self, generation, name, width, height):
        raise NotImplementedError

    def enter_background(self):
        raise NotImplementedError

    def resume(self):
        raise NotImplementedError

    def set_thermal(self, level):
        raise NotImplementedError
