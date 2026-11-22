"""VXLAN tunnel registry (skeleton)."""


class VxlanError(Exception):
    """Error carrying a stable VXLAN error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VxlanTunnelRegistry:
    """Registry of VNIs, VTEPs, MAC learning and ARP suppression."""

    def __init__(self, mtu_overhead=50, flap_limit=3, flap_window=60):
        self.mtu_overhead = mtu_overhead
        self.flap_limit = flap_limit
        self.flap_window = flap_window

    def register_vtep(self, vtep_id, ip):
        """Register a VTEP identity and its unique IP.

        Two VTEP identities sharing one IP raise
        VxlanError("VXLAN_VTEP_CONFLICT").
        """
        raise NotImplementedError

    def create_vni(self, vni, vtep_id, group=None):
        """Create a VNI on a registered VTEP.

        VNI 0 and 0xFFFFFF raise VxlanError("VXLAN_VNI_INVALID").  Recreating
        the same VNI on a different VTEP raises VxlanError("VXLAN_VNI_CONFLICT").
        Returns {"vni": int, "vtep_id": str, "created": bool}.
        """
        raise NotImplementedError

    def learn_mac(self, vni, mac, port):
        """Learn a MAC inside one VNI only.

        Returns {"vni": int, "mac": str, "port": str, "entries": int}; a MAC
        re-learned in the same VNI stays a single entry.
        """
        raise NotImplementedError

    def lookup_mac(self, vni, mac):
        """Return the port for a MAC in this VNI, or None."""
        raise NotImplementedError

    def cache_arp(self, vni, ip, mac):
        """Cache an ARP suppression mapping; returns {"cached": True}."""
        raise NotImplementedError

    def lookup_arp(self, vni, ip):
        """Return the suppressed MAC, or None."""
        raise NotImplementedError

    def delete_vni(self, vni):
        """Atomically remove forwarding entries and multicast membership.

        Returns {"deleted": bool, "forwarding_entries_removed": int,
        "multicast_memberships_removed": int}.
        """
        raise NotImplementedError

    def multicast_groups(self):
        """Return the current {vni: group} membership map."""
        raise NotImplementedError

    def encapsulate(self, payload_size):
        """Return {"inner_size": int, "outer_size": int, "overhead": int}."""
        raise NotImplementedError

    def source_port_hash(self, flow_id):
        """Return a stable source UDP port in the 49152-65535 range.

        Different flows must not all collapse onto one fixed port.
        """
        raise NotImplementedError

    def update_remote(self, vtep_id, new_ip, now):
        """Update a remote VTEP address with flap protection.

        More than ``flap_limit`` changes inside ``flap_window`` raise
        VxlanError("VXLAN_VTEP_FLAP") and keep the previous address.
        Returns {"ip": str, "changes": int}.
        """
        raise NotImplementedError

    def validate_encap(self, header):
        """Reject VXLAN headers with non-zero reserved bits.

        Raise VxlanError("VXLAN_RESERVED_BITS"); valid returns {"valid": True}.
        """
        raise NotImplementedError

    def is_ready(self):
        """Return True once forwarding state is usable (no flooding needed)."""
        raise NotImplementedError

    def flooding_required(self):
        """Return True while the forwarding table has not been restored."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize VTEPs, VNIs, MAC and ARP tables."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore state after a restart; forwarding must be ready at once."""
        raise NotImplementedError
