class SsaoError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SsaoKernelDenoiser:
    """Screen-space ambient occlusion over a depth buffer snapshot."""

    def __init__(self, radius=1.0, strength=1.0, min_ambient=0.2,
                 sample_count=8):
        self.radius = radius
        self.strength = strength
        self.min_ambient = min_ambient
        self.sample_count = sample_count

    def set_camera(self, near=0.1, far=100.0, fov=1.0, resolution=(4, 4)):
        raise NotImplementedError

    def set_depth(self, depth, version=1):
        raise NotImplementedError

    def set_normals(self, normals, version=1):
        raise NotImplementedError

    def set_sky(self, mask):
        raise NotImplementedError

    def set_transparent(self, mask):
        raise NotImplementedError

    def sample_kernel(self):
        raise NotImplementedError

    def generate(self, frame_version=None):
        raise NotImplementedError

    def shade(self, occlusion, intensity=1.0):
        raise NotImplementedError

    def swap_buffers(self, version):
        raise NotImplementedError

    def set_resolution(self, resolution):
        raise NotImplementedError
