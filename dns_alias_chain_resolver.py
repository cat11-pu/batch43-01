"""DNS alias-chain resolver with loop and trust guard (skeleton)."""


class DnsError(Exception):
    """Configuration failure raised by the resolver."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class DnsAliasChainResolver:
    """Resolves CNAME chains without escaping trust boundaries."""

    def __init__(self, allowed_zones=("example.com",), max_depth=16, udp_size=512):
        self.allowed_zones = tuple(allowed_zones)
        self.max_depth = max_depth
        self.udp_size = udp_size

    def add_record(self, name, rtype, value, ttl=60, rclass="IN", replace=False):
        raise NotImplementedError

    def resolve(self, name, rtype="A", now=0.0):
        raise NotImplementedError

    def cache_lookup(self, name, now=0.0):
        raise NotImplementedError

    def upstream_count(self):
        raise NotImplementedError
