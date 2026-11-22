"""VLAN access-boundary enforcement (skeleton)."""


class VlanError(Exception):
    """Configuration failure raised by the enforcer."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class VlanBoundaryEnforcer:
    """Blocks double tagging, DTP and address moves at access ports."""

    def __init__(self, state_path=None, freeze_window=2.0, default_max_mac=1):
        self.state_path = state_path
        self.freeze_window = freeze_window
        self.default_max_mac = default_max_mac

    def configure_access(self, port, vlan, native_vlan=None, max_mac=None, voice_vlan=None):
        raise NotImplementedError

    def frame(self, src_mac, vlan, port, tags=(), ethertype="data", now=0.0):
        raise NotImplementedError

    def is_frozen(self, port):
        raise NotImplementedError

    def release(self, port):
        raise NotImplementedError

    def port_down(self, port):
        raise NotImplementedError

    def sticky_table(self):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
