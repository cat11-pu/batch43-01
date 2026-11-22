class ResourceInUseError(RuntimeError):
    """Raised when a resource is recycled before its fence completes."""


class FenceError(ValueError):
    """Raised when a frame command operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FrameCommandFence:
    """Per-frame command recording, submission order and fence reuse."""

    def __init__(self, frames_in_flight=2):
        self.frames_in_flight = frames_in_flight

    def begin(self, frame_index):
        raise NotImplementedError

    def submit(self, frame_index, token=None, resources=()):
        raise NotImplementedError

    def signal(self, frame_index):
        raise NotImplementedError

    def wait(self, frame_index, timeout=0):
        raise NotImplementedError

    def acquire(self, resource, frame_index):
        raise NotImplementedError

    def recycle(self, resource):
        raise NotImplementedError

    def resize(self, width, height):
        raise NotImplementedError

    def device_lost(self):
        raise NotImplementedError

    def device_restored(self, width=None, height=None):
        raise NotImplementedError

    def shutdown(self, timeout=0):
        raise NotImplementedError

    def pending(self):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError
