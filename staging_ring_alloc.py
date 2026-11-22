class StagingError(ValueError):
    """Raised when a staging ring operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RingExhausted(StagingError):
    """Raised when no safe range can be allocated in the ring."""


class MisalignedUpload(StagingError):
    """Raised when a requested alignment violates the resource minimum."""


class StagingRingAlloc:
    """Frame-scoped staging ring with fence aware reuse and generations."""

    def __init__(self, capacity=1024):
        self.capacity = capacity

    def alloc(self, size, kind="buffer", frame=0, alignment=None, wait=False):
        raise NotImplementedError

    def write(self, marker, data, sub_offset=0, partial=False):
        raise NotImplementedError

    def read(self, marker):
        raise NotImplementedError

    def flush(self, frame):
        raise NotImplementedError

    def recycle(self, frame):
        raise NotImplementedError

    def complete(self, marker):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError

    def device_lost(self):
        raise NotImplementedError

    def device_restored(self):
        raise NotImplementedError
