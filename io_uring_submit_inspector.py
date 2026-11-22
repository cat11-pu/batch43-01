"""io_uring submission inspector (skeleton)."""


class IouringError(Exception):
    """Configuration failure raised by the inspector."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class IoUringSubmitInspector:
    """Validates io_uring submissions before they reach the kernel."""

    def __init__(
        self,
        allowed_opcodes=("READ", "WRITE", "TIMEOUT"),
        queue_depth=8,
        allowed_root="/sandbox",
        max_fixed_files=4,
    ):
        self.allowed_opcodes = tuple(allowed_opcodes)
        self.queue_depth = queue_depth
        self.allowed_root = allowed_root
        self.max_fixed_files = max_fixed_files

    def register_buffers(self, buffers):
        raise NotImplementedError

    def register_files(self, paths):
        raise NotImplementedError

    def register_identity(self, identity):
        raise NotImplementedError

    def submit(self, request):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def completions(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
