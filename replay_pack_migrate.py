class ReplayError(ValueError):
    """Raised when a replay pack migration is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReplayPackMigrate:
    """Replay pack inspection, atomic migration, verification and rollback."""

    def __init__(self, defaults=None, target_version=3, supported=(1, 2, 3)):
        self.defaults = defaults or {}
        self.target_version = target_version
        self.supported = tuple(supported)

    def inspect(self, path):
        raise NotImplementedError

    def migrate(self, source, target=None, crash_after=None):
        raise NotImplementedError

    def verify(self, path):
        raise NotImplementedError

    def rollback(self, path):
        raise NotImplementedError
