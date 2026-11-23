"""VXLAN tunnel registry with per-VNI forwarding state."""

import copy
import hashlib
import ipaddress
import json
import threading
from collections import deque


class VxlanError(Exception):
    """Error carrying a stable VXLAN error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VxlanTunnelRegistry:
    """Registry of VNIs, VTEPs, MAC learning and ARP suppression."""

    MIN_VNI = 1
    MAX_VNI = 0xFFFFFE
    MIN_SOURCE_PORT = 49152
    MAX_SOURCE_PORT = 65535

    def __init__(self, mtu_overhead=50, flap_limit=3, flap_window=60):
        self.mtu_overhead = mtu_overhead
        self.flap_limit = flap_limit
        self.flap_window = flap_window

        self._lock = threading.RLock()
        self._vteps = {}
        self._vtep_by_ip = {}
        self._remote_vteps = {}
        self._remote_changes = {}
        self._vnis = {}
        self._multicast = {}
        self._ready = False
        self._flooding_required = True

    @staticmethod
    def _ip_key(ip):
        text = str(ip).strip()
        try:
            return str(ipaddress.ip_address(text))
        except ValueError:
            return text.lower()

    @classmethod
    def _validate_vni(cls, vni):
        if isinstance(vni, bool) or not isinstance(vni, int):
            raise VxlanError("VXLAN_VNI_INVALID", "VNI must be an integer")
        if not cls.MIN_VNI <= vni <= cls.MAX_VNI:
            raise VxlanError(
                "VXLAN_VNI_INVALID",
                "VNI must be in the range 1..0xFFFFFE",
            )
        return vni

    @staticmethod
    def _normalize_mac(mac):
        if isinstance(mac, (bytes, bytearray)):
            if len(mac) != 6:
                raise ValueError("MAC address must contain six octets")
            return ":".join(f"{octet:02x}" for octet in mac)

        text = str(mac).strip().lower().replace("-", ":")
        octets = text.split(":")
        if len(octets) != 6:
            raise ValueError("MAC address must contain six octets")

        normalized = []
        for octet in octets:
            if not 1 <= len(octet) <= 2:
                raise ValueError("invalid MAC address")
            value = int(octet, 16)
            normalized.append(f"{value:02x}")
        return ":".join(normalized)

    @staticmethod
    def _validate_reserved_value(value):
        if isinstance(value, str):
            return int(value, 0)
        if isinstance(value, (bytes, bytearray)):
            return int.from_bytes(value, "big")
        return int(value)

    def register_vtep(self, vtep_id, ip):
        """Register a VTEP identity and its unique IP.

        Two VTEP identities sharing one IP raise
        VxlanError("VXLAN_VTEP_CONFLICT").
        """
        with self._lock:
            if vtep_id in self._vteps:
                if self._ip_key(self._vteps[vtep_id]) == self._ip_key(ip):
                    return {
                        "vtep_id": vtep_id,
                        "ip": self._vteps[vtep_id],
                        "created": False,
                    }
                raise VxlanError(
                    "VXLAN_VTEP_CONFLICT",
                    "VTEP identity already has a different IP",
                )

            ip_key = self._ip_key(ip)
            owner = self._vtep_by_ip.get(ip_key)
            if owner is not None and owner != vtep_id:
                raise VxlanError(
                    "VXLAN_VTEP_CONFLICT",
                    f"IP {ip} is already assigned to {owner}",
                )

            self._vteps[vtep_id] = ip
            self._vtep_by_ip[ip_key] = vtep_id
            return {"vtep_id": vtep_id, "ip": ip, "created": True}

    def create_vni(self, vni, vtep_id, group=None):
        """Create a VNI on a registered VTEP.

        VNI 0 and 0xFFFFFF raise VxlanError("VXLAN_VNI_INVALID").  Recreating
        the same VNI on a different VTEP raises VxlanError("VXLAN_VNI_CONFLICT").
        Returns {"vni": int, "vtep_id": str, "created": bool}.
        """
        self._validate_vni(vni)

        with self._lock:
            if vtep_id not in self._vteps:
                raise VxlanError("VXLAN_VTEP_NOT_FOUND", "VTEP is not registered")

            current = self._vnis.get(vni)
            if current is not None:
                if current["vtep_id"] != vtep_id:
                    raise VxlanError(
                        "VXLAN_VNI_CONFLICT",
                        "VNI already belongs to another VTEP",
                    )

                if group is not None:
                    current["group"] = group
                    self._multicast[vni] = group

                return {
                    "vni": vni,
                    "vtep_id": vtep_id,
                    "created": False,
                }

            self._vnis[vni] = {
                "vtep_id": vtep_id,
                "group": group,
                "macs": {},
                "arp": {},
            }
            if group is not None:
                self._multicast[vni] = group

            return {"vni": vni, "vtep_id": vtep_id, "created": True}

    def learn_mac(self, vni, mac, port):
        """Learn a MAC inside one VNI only.

        Returns {"vni": int, "mac": str, "port": str, "entries": int}; a MAC
        re-learned in the same VNI stays a single entry.
        """
        self._validate_vni(vni)
        normalized_mac = self._normalize_mac(mac)

        with self._lock:
            vni_state = self._vnis.get(vni)
            if vni_state is None:
                raise VxlanError("VXLAN_VNI_NOT_FOUND", "VNI is not registered")

            vni_state["macs"][normalized_mac] = port
            return {
                "vni": vni,
                "mac": normalized_mac,
                "port": port,
                "entries": len(vni_state["macs"]),
            }

    def lookup_mac(self, vni, mac):
        """Return the port for a MAC in this VNI, or None."""
        self._validate_vni(vni)
        try:
            normalized_mac = self._normalize_mac(mac)
        except (TypeError, ValueError):
            return None

        with self._lock:
            vni_state = self._vnis.get(vni)
            if vni_state is None:
                return None
            return vni_state["macs"].get(normalized_mac)

    def cache_arp(self, vni, ip, mac):
        """Cache an ARP suppression mapping; returns {"cached": True}."""
        self._validate_vni(vni)
        normalized_mac = self._normalize_mac(mac)
        ip_key = self._ip_key(ip)

        with self._lock:
            vni_state = self._vnis.get(vni)
            if vni_state is None:
                raise VxlanError("VXLAN_VNI_NOT_FOUND", "VNI is not registered")
            vni_state["arp"][ip_key] = normalized_mac
            return {"cached": True}

    def lookup_arp(self, vni, ip):
        """Return the suppressed MAC, or None."""
        self._validate_vni(vni)
        ip_key = self._ip_key(ip)
        with self._lock:
            vni_state = self._vnis.get(vni)
            if vni_state is None:
                return None
            return vni_state["arp"].get(ip_key)

    def delete_vni(self, vni):
        """Atomically remove forwarding entries and multicast membership.

        Returns {"deleted": bool, "forwarding_entries_removed": int,
        "multicast_memberships_removed": int}.
        """
        self._validate_vni(vni)
        with self._lock:
            vni_state = self._vnis.pop(vni, None)
            if vni_state is None:
                return {
                    "deleted": False,
                    "forwarding_entries_removed": 0,
                    "multicast_memberships_removed": 0,
                }

            multicast_removed = int(vni in self._multicast)
            self._multicast.pop(vni, None)
            return {
                "deleted": True,
                "forwarding_entries_removed": len(vni_state["macs"]),
                "multicast_memberships_removed": multicast_removed,
            }

    def multicast_groups(self):
        """Return the current {vni: group} membership map."""
        with self._lock:
            return dict(self._multicast)

    def encapsulate(self, payload_size):
        """Return {"inner_size": int, "outer_size": int, "overhead": int}."""
        if isinstance(payload_size, bool) or not isinstance(payload_size, int):
            raise ValueError("payload size must be an integer")
        if payload_size < 0:
            raise ValueError("payload size cannot be negative")

        return {
            "inner_size": payload_size,
            "outer_size": payload_size + self.mtu_overhead,
            "overhead": self.mtu_overhead,
        }

    def source_port_hash(self, flow_id):
        """Return a stable source UDP port in the 49152-65535 range.

        Different flows must not all collapse onto one fixed port.
        """
        digest = hashlib.blake2b(
            str(flow_id).encode("utf-8"),
            digest_size=8,
        ).digest()
        port_count = self.MAX_SOURCE_PORT - self.MIN_SOURCE_PORT + 1
        return self.MIN_SOURCE_PORT + int.from_bytes(digest, "big") % port_count

    def update_remote(self, vtep_id, new_ip, now):
        """Update a remote VTEP address with flap protection.

        More than ``flap_limit`` changes inside ``flap_window`` raise
        VxlanError("VXLAN_VTEP_FLAP") and keep the previous address.
        Returns {"ip": str, "changes": int}.
        """
        with self._lock:
            history = self._remote_changes.setdefault(vtep_id, deque())
            cutoff = now - self.flap_window
            while history and history[0] <= cutoff:
                history.popleft()

            old_ip = self._vteps.get(vtep_id)
            if old_ip is not None and self._ip_key(old_ip) == self._ip_key(new_ip):
                return {"ip": old_ip, "changes": len(history)}

            if len(history) >= self.flap_limit:
                raise VxlanError(
                    "VXLAN_VTEP_FLAP",
                    "VTEP address changed too often",
                )

            new_ip_key = self._ip_key(new_ip)
            owner = self._vtep_by_ip.get(new_ip_key)
            if owner is not None and owner != vtep_id:
                raise VxlanError(
                    "VXLAN_VTEP_CONFLICT",
                    f"IP {new_ip} is already assigned to {owner}",
                )

            if old_ip is not None:
                self._vtep_by_ip.pop(self._ip_key(old_ip), None)
            self._vteps[vtep_id] = new_ip
            self._vtep_by_ip[new_ip_key] = vtep_id
            self._remote_vteps[vtep_id] = new_ip
            history.append(now)

            return {"ip": new_ip, "changes": len(history)}

    def validate_encap(self, header):
        """Reject VXLAN headers with non-zero reserved bits.

        Raise VxlanError("VXLAN_RESERVED_BITS"); valid returns {"valid": True}.
        """
        if not isinstance(header, dict):
            raise VxlanError("VXLAN_RESERVED_BITS", "header must be a mapping")

        if "vni" in header:
            self._validate_vni(header["vni"])

        for key in ("reserved_bits", "reserved1", "reserved2", "reserved"):
            if key in header:
                try:
                    reserved = self._validate_reserved_value(header[key])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise VxlanError(
                        "VXLAN_RESERVED_BITS",
                        "reserved bits are malformed",
                    ) from exc
                if reserved != 0:
                    raise VxlanError(
                        "VXLAN_RESERVED_BITS",
                        "reserved bits must be zero",
                    )

        return {"valid": True}

    def is_ready(self):
        """Return True once forwarding state is usable (no flooding needed)."""
        with self._lock:
            return self._ready

    def flooding_required(self):
        """Return True while the forwarding table has not been restored."""
        with self._lock:
            return self._flooding_required

    def dump_state(self):
        """Serialize VTEPs, VNIs, MAC and ARP tables."""
        with self._lock:
            return {
                "version": 1,
                "vteps": copy.deepcopy(self._vteps),
                "remote_vteps": {
                    vtep_id: {
                        "ip": ip,
                        "change_times": list(self._remote_changes.get(vtep_id, ())),
                    }
                    for vtep_id, ip in self._remote_vteps.items()
                },
                "vnis": {
                    vni: {
                        "vtep_id": state["vtep_id"],
                        "group": state["group"],
                        "macs": copy.deepcopy(state["macs"]),
                        "arp": copy.deepcopy(state["arp"]),
                    }
                    for vni, state in self._vnis.items()
                },
                "multicast_groups": dict(self._multicast),
            }

    def load_state(self, state):
        """Restore state after a restart; forwarding must be ready at once."""
        if isinstance(state, (str, bytes, bytearray)):
            try:
                state = json.loads(state)
            except (TypeError, ValueError) as exc:
                raise VxlanError("VXLAN_STATE_INVALID", "invalid JSON state") from exc

        try:
            if not isinstance(state, dict):
                raise ValueError("state must be a mapping")

            vteps = copy.deepcopy(state.get("vteps", {}))
            if not isinstance(vteps, dict):
                raise ValueError("vteps must be a mapping")

            vtep_by_ip = {}
            for vtep_id, ip in vteps.items():
                ip_key = self._ip_key(ip)
                owner = vtep_by_ip.get(ip_key)
                if owner is not None and owner != vtep_id:
                    raise ValueError("duplicate VTEP IP")
                vtep_by_ip[ip_key] = vtep_id

            raw_vnis = state.get("vnis", {})
            if not isinstance(raw_vnis, dict):
                raise ValueError("vnis must be a mapping")

            vnis = {}
            multicast = {}
            for raw_vni, raw_vni_state in raw_vnis.items():
                vni = int(raw_vni)
                self._validate_vni(vni)
                if not isinstance(raw_vni_state, dict):
                    raise ValueError("VNI state must be a mapping")

                vtep_id = raw_vni_state.get("vtep_id")
                if vtep_id not in vteps:
                    raise ValueError("VNI references an unknown VTEP")

                group = raw_vni_state.get("group")
                raw_macs = raw_vni_state.get("macs", {})
                raw_arp = raw_vni_state.get("arp", {})
                if not isinstance(raw_macs, dict) or not isinstance(raw_arp, dict):
                    raise ValueError("MAC and ARP tables must be mappings")

                macs = {}
                for mac, port in raw_macs.items():
                    macs[self._normalize_mac(mac)] = port

                arp = {}
                for ip, mac in raw_arp.items():
                    arp[self._ip_key(ip)] = self._normalize_mac(mac)

                vnis[vni] = {
                    "vtep_id": vtep_id,
                    "group": group,
                    "macs": macs,
                    "arp": arp,
                }
                if group is not None:
                    multicast[vni] = group

            remote_vteps = {}
            remote_changes = {}
            raw_remote = state.get("remote_vteps", {})
            if not isinstance(raw_remote, dict):
                raise ValueError("remote_vteps must be a mapping")

            for vtep_id, remote_state in raw_remote.items():
                if isinstance(remote_state, dict):
                    ip = remote_state.get("ip", vteps.get(vtep_id))
                    change_times = remote_state.get("change_times", ())
                else:
                    ip = remote_state
                    change_times = ()

                if ip is None or vtep_id not in vteps:
                    raise ValueError("remote VTEP state is incomplete")
                remote_vteps[vtep_id] = ip
                remote_changes[vtep_id] = deque(change_times)
        except VxlanError:
            raise
        except (TypeError, ValueError, OverflowError) as exc:
            raise VxlanError("VXLAN_STATE_INVALID", str(exc)) from exc

        with self._lock:
            self._vteps = vteps
            self._vtep_by_ip = vtep_by_ip
            self._vnis = vnis
            self._multicast = multicast
            self._remote_vteps = remote_vteps
            self._remote_changes = remote_changes
            self._ready = True
            self._flooding_required = False
