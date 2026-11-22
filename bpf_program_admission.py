"""BPF program admission and tenant isolation (skeleton)."""


class BpfError(Exception):
    """Configuration failure raised by the admission controller."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class BpfProgramAdmission:
    """Validates helper usage, map pins and tail calls before loading BPF."""

    def __init__(
        self,
        tenant="tenant-a",
        allowed_helpers=None,
        map_capacity=64,
        cgroup_root="/sys/fs/cgroup/tenant-a",
    ):
        self.tenant = tenant
        self.allowed_helpers = allowed_helpers
        self.map_capacity = map_capacity
        self.cgroup_root = cgroup_root

    def load(self, program, now=0.0):
        raise NotImplementedError

    def unload(self, program_id):
        raise NotImplementedError

    def attach(self, program_id):
        raise NotImplementedError

    def pin_path(self, program_id):
        raise NotImplementedError

    def loaded(self):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
