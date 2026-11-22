"""DHCPv6 prefix delegation broker (skeleton)."""


class Dhcpv6Error(Exception):
    """Binding or configuration failure raised by the broker."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class Dhcpv6PdBroker:
    """Allocates and tracks delegated IPv6 prefixes per DUID and IAID."""

    def __init__(
        self,
        pool="2001:db8::/48",
        prefix_length=56,
        state_path=None,
        valid_lifetime=100,
        preferred_lifetime=50,
        max_hops=2,
        capacity=None,
    ):
        self.pool = pool
        self.prefix_length = prefix_length
        self.state_path = state_path
        self.valid_lifetime = valid_lifetime
        self.preferred_lifetime = preferred_lifetime
        self.max_hops = max_hops
        self.capacity = capacity

    def request(
        self,
        duid,
        iaid,
        link=None,
        hop_count=0,
        prefix_length=None,
        rapid_commit=False,
        now=0.0,
        count=1,
    ):
        raise NotImplementedError

    def renew(self, duid, iaid, now=0.0):
        raise NotImplementedError

    def release(self, duid, iaid, now=0.0):
        raise NotImplementedError

    def usage(self, now=0.0):
        raise NotImplementedError
