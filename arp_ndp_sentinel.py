"""ARP / NDP sentinel for access-layer spoofing protection (skeleton)."""


class SentinelError(Exception):
    """Configuration failure raised by the sentinel."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class ArpNdpSentinel:
    """Protects static and snooped bindings from ARP and NDP spoofing."""

    def __init__(self, state_path=None, isolation_seconds=60, isolation_window=1.0):
        self.state_path = state_path
        self.isolation_seconds = isolation_seconds
        self.isolation_window = isolation_window

    def add_static(self, vlan, ip, mac, port=None):
        raise NotImplementedError

    def add_snooping(self, vlan, ip, mac, port):
        raise NotImplementedError

    def add_allowlist(self, mac):
        raise NotImplementedError

    def observe_arp(self, vlan, ip, mac, port, gratuitous=False, now=0.0):
        raise NotImplementedError

    def observe_ndp(
        self,
        vlan,
        ip,
        mac,
        port,
        router_flag=False,
        override_flag=False,
        src_unspecified=False,
        now=0.0,
    ):
        raise NotImplementedError

    def is_blocked(self, port):
        raise NotImplementedError

    def release_port(self, port):
        raise NotImplementedError

    def table(self, vlan):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
