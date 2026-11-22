"""NAT64 session binder (skeleton)."""


class Nat64Error(Exception):
    """Error carrying a stable NAT64 error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class Nat64SessionBinder:
    """Binds IPv6 flows to NAT64 mappings and keeps state across restarts."""

    def __init__(self, public_address="2001:db8::1",
                 internal_prefix="192.168.0.0/16", port_min=1024,
                 port_max=65535, tcp_timeout=7200, udp_timeout=300,
                 icmp_timeout=60):
        self.public_address = public_address
        self.internal_prefix = internal_prefix
        self.port_min = port_min
        self.port_max = port_max
        self.tcp_timeout = tcp_timeout
        self.udp_timeout = udp_timeout
        self.icmp_timeout = icmp_timeout

    def create_session(self, src_ip, src_port, dst_ip, dst_port, protocol,
                       now=0, xlat464=False, requested_port=None):
        """Create or reuse a mapping for the exact five-tuple.

        Returns {"mapping_id": str, "public_address": str, "public_port": int,
        "protocol": str, "hairpin": bool}.  Identical tuples reuse the same
        mapping; different source ports get different mappings.  464XLAT must
        keep the source port parity; a mismatched requested port raises
        Nat64Error("NAT64_PORT_PARITY").  Traffic to an internal destination is
        hairpinned with a second translation and also reports "translated" and
        "inbound_port".
        """
        raise NotImplementedError

    def lookup_session(self, src_ip, src_port, dst_ip, dst_port, protocol):
        """Return the mapping dict for the five-tuple, or None."""
        raise NotImplementedError

    def remaining_ttl(self, mapping_id, now):
        """Return the remaining lifetime in seconds for a mapping."""
        raise NotImplementedError

    def handle_icmp_error(self, inner_src_ip, inner_src_port, inner_dst_ip,
                          inner_dst_port, protocol):
        """Reverse-lookup the session carried inside an ICMP error.

        Returns {"mapping_id": str, "delivered": True}.  Unknown flows raise
        Nat64Error("NAT64_NO_SESSION") and must be dropped.
        """
        raise NotImplementedError

    def handle_tcp_reset(self, src_ip, src_port, dst_ip, dst_port):
        """Remove a TCP mapping when a reset is seen.

        Returns {"removed": bool}.
        """
        raise NotImplementedError

    def translate_fragment(self, mapping_id, offset, more_fragments, payload):
        """Reassemble fragments before translating.

        Non-final fragments return {"buffered": True}.  The final fragment
        returns {"translated": True, "offset": int, "total_size": int} with the
        original fragment offset preserved.
        """
        raise NotImplementedError

    def should_expire(self, protocol, idle_seconds):
        """Return True when the protocol-specific idle timeout is reached."""
        raise NotImplementedError

    def expire(self, now):
        """Expire aged mappings; return {"expired": int}."""
        raise NotImplementedError

    def synthesize_dns64(self, ipv4, scope="global"):
        """Synthesize a DNS64 address carrying its scope; return the address."""
        raise NotImplementedError

    def dns64_usable(self, address, scope):
        """Return False when a synthesized address is used outside its scope."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize mappings, ports and remaining lifetimes."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore mappings and their remaining lifetime after a restart."""
        raise NotImplementedError
