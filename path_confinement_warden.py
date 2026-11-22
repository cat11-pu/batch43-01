"""Root-confined path resolution and atomic writes (skeleton)."""


class PathConfinementError(Exception):
    """Base failure raised by the warden."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class PathEscapeError(PathConfinementError):
    """Raised when a path escapes the confined root."""


class PathNotBeneath(PathConfinementError):
    """Raised for absolute paths or parent-directory components."""


class PathConfinementWarden:
    """Resolves and writes paths without ever leaving the provided root."""

    def __init__(self, max_path_length=255):
        self.max_path_length = max_path_length

    def resolve(self, root, relative, symlinks=None):
        raise NotImplementedError

    def write_file(self, root, relative, data, fail_after_temp=False):
        raise NotImplementedError

    def extract(self, root, entries):
        raise NotImplementedError
