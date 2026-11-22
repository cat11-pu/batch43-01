"""Path MTU discovery probe and cache (skeleton)."""


class PmtudError(Exception):
    """Cache or configuration failure raised by the probe."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class PmtudPathMtuProbe:
    """Verifies ICMP PMTU reports and maintains per-path MTU values."""

    def __init__(self, default_mtu=1500, cache_ttl=60.0, verify_window=30.0):
        self.default_mtu = default_mtu
        self.cache_ttl = cache_ttl
        self.verify_window = verify_window

    def send(self, flow, dest, size, tunnel=None, df=True, inner_df=True, now=0.0):
        raise NotImplementedError

    def on_icmp(
        self,
        flow,
        dest,
        tunnel=None,
        reported_mtu=1280,
        quoted_bytes=8,
        icmp_type=3,
        icmp_code=4,
        now=0.0,
    ):
        raise NotImplementedError

    def mtu_for(self, dest, tunnel=None, now=0.0):
        raise NotImplementedError

    def clamp_mss(self, dest, mss, tunnel=None, direction="out", now=0.0):
        raise NotImplementedError

    def route_change(self, dest):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state, now=0.0):
        raise NotImplementedError

    def report_application_timeout(self, dest):
        raise NotImplementedError
