"""IPv6 extension-header and fragment filter (skeleton)."""


class Ipv6FilterError(Exception):
    """Configuration failure raised by the filter."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class Ipv6ExtFilter:
    """Validates IPv6 extension chains, fragments and ICMPv6 errors."""

    def __init__(self, max_ext_headers=8):
        self.max_ext_headers = max_ext_headers

    def register_flow(self, src, dst, sport, dport, proto="tcp"):
        raise NotImplementedError

    def filter_packet(
        self,
        headers=(),
        src="2001:db8::1",
        dst="2001:db8::2",
        fragment=None,
        flow_label=0,
        icmp_error=None,
        payload_len=0,
    ):
        raise NotImplementedError

    def reassemble(self, src, ident, offset, payload, more=True):
        raise NotImplementedError
