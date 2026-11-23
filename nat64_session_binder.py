"""NAT64 session binder."""

import hashlib
import ipaddress
import json
import threading


class Nat64Error(Exception):
    """Error carrying a stable NAT64 error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class Nat64SessionBinder:
    """Binds IPv6 flows to NAT64 mappings and keeps state across restarts."""

    _PROTOCOL_ALIASES = {
        "icmpv6": "icmp",
        "ipv6-icmp": "icmp",
    }

    def __init__(self, public_address="2001:db8::1",
                 internal_prefix="192.168.0.0/16", port_min=1024,
                 port_max=65535, tcp_timeout=7200, udp_timeout=300,
                 icmp_timeout=60):
        self.public_address = public_address
        self.internal_prefix = internal_prefix
        self.port_min = int(port_min)
        self.port_max = int(port_max)
        self.tcp_timeout = tcp_timeout
        self.udp_timeout = udp_timeout
        self.icmp_timeout = icmp_timeout

        self._internal_network = self._parse_internal_network(internal_prefix)
        self._lock = threading.RLock()
        self._sessions = {}
        self._mappings = {}
        self._aliases = {}
        self._mapping_aliases = {}
        self._allocated_ports = set()
        self._next_port = self.port_min
        self._next_mapping = 1
        self._fragments = {}
        self._last_now = 0

    @staticmethod
    def _parse_internal_network(prefix):
        try:
            return ipaddress.ip_network(prefix, strict=False)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalize_protocol(cls, protocol):
        value = str(protocol).strip().lower()
        return cls._PROTOCOL_ALIASES.get(value, value)

    @staticmethod
    def _port_number(port):
        try:
            return int(port)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _session_key(cls, src_ip, src_port, dst_ip, dst_port, protocol):
        return (
            str(src_ip),
            cls._port_number(src_port),
            str(dst_ip),
            cls._port_number(dst_port),
            cls._normalize_protocol(protocol),
        )

    def _timeout_for(self, protocol):
        protocol = self._normalize_protocol(protocol)
        if protocol == "tcp":
            return self.tcp_timeout
        if protocol in ("udp", "udplite"):
            return self.udp_timeout
        if protocol in ("icmp", "icmpv6"):
            return self.icmp_timeout
        return self.tcp_timeout

    def _is_internal(self, address):
        if self._internal_network is None:
            return False
        try:
            return ipaddress.ip_address(str(address)) in self._internal_network
        except ValueError:
            return False

    @staticmethod
    def _embedded_ipv4(address):
        try:
            parsed = ipaddress.IPv6Address(str(address))
        except (ipaddress.AddressValueError, ValueError):
            return None
        if parsed.packed[:4] != b"\x00\x64\xff\x9b":
            return None
        return str(ipaddress.IPv4Address(int(parsed) & 0xffffffff))

    def _alias_keys(self, mapping):
        protocol = mapping["protocol"]
        src = (
            mapping["src_ip"],
            mapping["src_port"],
            mapping["dst_ip"],
            mapping["dst_port"],
            protocol,
        )
        reverse = (
            mapping["dst_ip"],
            mapping["dst_port"],
            mapping["src_ip"],
            mapping["src_port"],
            protocol,
        )
        public_out = (
            mapping["public_address"],
            mapping["public_port"],
            mapping["dst_ip"],
            mapping["dst_port"],
            protocol,
        )
        public_in = (
            mapping["dst_ip"],
            mapping["dst_port"],
            mapping["public_address"],
            mapping["public_port"],
            protocol,
        )
        aliases = {src, reverse, public_out, public_in}
        if mapping.get("inbound_port") is not None:
            hairpin_out = (
                mapping["public_address"],
                mapping["inbound_port"],
                mapping["dst_ip"],
                mapping["dst_port"],
                protocol,
            )
            hairpin_in = (
                mapping["dst_ip"],
                mapping["dst_port"],
                mapping["public_address"],
                mapping["inbound_port"],
                protocol,
            )
            aliases.update((hairpin_out, hairpin_in))
        return aliases

    def _index_mapping(self, mapping):
        mapping_id = mapping["mapping_id"]
        aliases = self._alias_keys(mapping)
        self._mapping_aliases[mapping_id] = aliases
        for key in aliases:
            self._aliases.setdefault(key, set()).add(mapping_id)

    def _unindex_mapping(self, mapping_id):
        for key in self._mapping_aliases.pop(mapping_id, set()):
            ids = self._aliases.get(key)
            if ids is None:
                continue
            ids.discard(mapping_id)
            if not ids:
                self._aliases.pop(key, None)

    def _release_mapping_port(self, mapping):
        for field in ("public_port", "inbound_port"):
            port = mapping.get(field)
            if port is not None:
                self._allocated_ports.discard(port)

    def _remove_mapping(self, mapping_id):
        mapping = self._mappings.pop(mapping_id, None)
        if mapping is None:
            return False
        self._sessions.pop(self._session_key(
            mapping["src_ip"], mapping["src_port"],
            mapping["dst_ip"], mapping["dst_port"],
            mapping["protocol"],
        ), None)
        self._unindex_mapping(mapping_id)
        self._release_mapping_port(mapping)
        self._fragments.pop(mapping_id, None)
        return True

    def _resolve_mapping(self, src_ip, src_port, dst_ip, dst_port, protocol):
        key = self._session_key(
            src_ip, src_port, dst_ip, dst_port, protocol
        )
        mapping_id = self._sessions.get(key)
        if mapping_id is not None:
            return self._mappings.get(mapping_id)

        candidates = self._aliases.get(key, ())
        for candidate_id in sorted(candidates):
            mapping = self._mappings.get(candidate_id)
            if mapping is not None:
                return mapping

        translated_src = self._embedded_ipv4(src_ip)
        translated_dst = self._embedded_ipv4(dst_ip)
        if translated_src is not None or translated_dst is not None:
            key = self._session_key(
                translated_src or src_ip,
                src_port,
                translated_dst or dst_ip,
                dst_port,
                protocol,
            )
            mapping_id = self._sessions.get(key)
            if mapping_id is not None:
                return self._mappings.get(mapping_id)
            for candidate_id in sorted(self._aliases.get(key, ())):
                mapping = self._mappings.get(candidate_id)
                if mapping is not None:
                    return mapping
        return None

    def _allocate_port(self, parity=None, preferred=None):
        if self.port_min > self.port_max:
            raise Nat64Error(
                "NAT64_PORT_EXHAUSTED", "invalid port range"
            )

        if preferred is not None:
            try:
                port = int(preferred)
            except (TypeError, ValueError):
                raise Nat64Error(
                    "NAT64_PORT_EXHAUSTED", "invalid requested port"
                )
            if port < self.port_min or port > self.port_max:
                raise Nat64Error(
                    "NAT64_PORT_EXHAUSTED", "requested port out of range"
                )
            if port in self._allocated_ports:
                raise Nat64Error(
                    "NAT64_PORT_EXHAUSTED", "requested port is in use"
                )
            self._allocated_ports.add(port)
            self._next_port = (
                port + 1 if port < self.port_max else self.port_min
            )
            return port

        span = self.port_max - self.port_min + 1
        start = self._next_port
        if start < self.port_min or start > self.port_max:
            start = self.port_min
        for index in range(span):
            port = self.port_min + ((start - self.port_min + index) % span)
            if parity is not None and port % 2 != parity:
                continue
            if port in self._allocated_ports:
                continue
            self._allocated_ports.add(port)
            self._next_port = (
                port + 1 if port < self.port_max else self.port_min
            )
            return port
        raise Nat64Error(
            "NAT64_PORT_EXHAUSTED", "no free port with required parity"
        )

    @staticmethod
    def _session_view(mapping):
        result = {
            "mapping_id": mapping["mapping_id"],
            "public_address": mapping["public_address"],
            "public_port": mapping["public_port"],
            "protocol": mapping["protocol"],
            "hairpin": mapping["hairpin"],
        }
        if mapping["hairpin"]:
            result["translated"] = True
            result["inbound_port"] = mapping["inbound_port"]
        return result

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
        protocol = self._normalize_protocol(protocol)
        src_port_value = self._port_number(src_port)
        dst_port_value = self._port_number(dst_port)
        key = self._session_key(
            src_ip, src_port_value, dst_ip, dst_port_value, protocol
        )

        with self._lock:
            self._last_now = max(self._last_now, now)
            if xlat464 and requested_port is not None:
                if self._port_number(requested_port) % 2 != (
                        src_port_value % 2):
                    raise Nat64Error(
                        "NAT64_PORT_PARITY",
                        "requested port parity differs from source port",
                    )

            existing_id = self._sessions.get(key)
            if existing_id is not None:
                existing = self._mappings[existing_id]
                if xlat464 and existing["public_port"] % 2 != (
                        src_port_value % 2):
                    raise Nat64Error(
                        "NAT64_PORT_PARITY",
                        "existing mapping does not preserve port parity",
                    )
                if now > existing["last_seen"]:
                    existing["last_seen"] = now
                    existing["expires_at"] = now + existing["timeout"]
                return self._session_view(existing)

            parity = src_port_value % 2 if xlat464 else None
            public_port = self._allocate_port(
                parity=parity, preferred=requested_port
            )
            hairpin = self._is_internal(dst_ip)
            inbound_port = None
            try:
                if hairpin:
                    inbound_port = self._allocate_port()
            except Exception:
                self._allocated_ports.discard(public_port)
                raise

            timeout = self._timeout_for(protocol)
            mapping_id = "mapping-{}".format(self._next_mapping)
            self._next_mapping += 1
            mapping = {
                "mapping_id": mapping_id,
                "src_ip": str(src_ip),
                "src_port": src_port_value,
                "dst_ip": str(dst_ip),
                "dst_port": dst_port_value,
                "protocol": protocol,
                "public_address": self.public_address,
                "public_port": public_port,
                "hairpin": hairpin,
                "translated": hairpin,
                "inbound_port": inbound_port,
                "xlat464": bool(xlat464),
                "created_at": now,
                "last_seen": now,
                "timeout": timeout,
                "expires_at": now + timeout,
            }
            self._mappings[mapping_id] = mapping
            self._sessions[key] = mapping_id
            self._index_mapping(mapping)
            return self._session_view(mapping)

    def lookup_session(self, src_ip, src_port, dst_ip, dst_port, protocol,
                       now=None):
        """Return the mapping dict for the five-tuple, or None."""
        with self._lock:
            mapping = self._resolve_mapping(
                src_ip, src_port, dst_ip, dst_port, protocol
            )
            if mapping is None:
                return None
            if now is not None and now > mapping["last_seen"]:
                mapping["last_seen"] = now
                mapping["expires_at"] = now + mapping["timeout"]
                self._last_now = max(self._last_now, now)
            return dict(mapping)

    def remaining_ttl(self, mapping_id, now):
        """Return the remaining lifetime in seconds for a mapping."""
        with self._lock:
            mapping = self._mappings.get(mapping_id)
            if mapping is None:
                return 0
            idle = max(0, now - mapping["last_seen"])
            return max(0, int(mapping["timeout"] - idle))

    def handle_icmp_error(self, inner_src_ip, inner_src_port, inner_dst_ip,
                          inner_dst_port, protocol, now=None):
        """Reverse-lookup the session carried inside an ICMP error.

        Returns {"mapping_id": str, "delivered": True}.  Unknown flows raise
        Nat64Error("NAT64_NO_SESSION") and must be dropped.
        """
        with self._lock:
            mapping = self._resolve_mapping(
                inner_src_ip, inner_src_port, inner_dst_ip, inner_dst_port,
                protocol,
            )
            if mapping is None:
                raise Nat64Error(
                    "NAT64_NO_SESSION", "ICMP error has no matching session"
                )
            if now is not None and now > mapping["last_seen"]:
                mapping["last_seen"] = now
                mapping["expires_at"] = now + mapping["timeout"]
                self._last_now = max(self._last_now, now)
            return {
                "mapping_id": mapping["mapping_id"],
                "delivered": True,
            }

    def handle_tcp_reset(self, src_ip, src_port, dst_ip, dst_port):
        """Remove a TCP mapping when a reset is seen.

        Returns {"removed": bool}.
        """
        with self._lock:
            mapping = self._resolve_mapping(
                src_ip, src_port, dst_ip, dst_port, "tcp"
            )
            if mapping is None:
                return {"removed": False}
            self._remove_mapping(mapping["mapping_id"])
            return {"removed": True}

    def translate_fragment(self, mapping_id, offset, more_fragments, payload):
        """Reassemble fragments before translating.

        Non-final fragments return {"buffered": True}.  The final fragment
        returns {"translated": True, "offset": int, "total_size": int} with the
        original fragment offset preserved.
        """
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            raise Nat64Error(
                "NAT64_INVALID_FRAGMENT", "fragment offset is not an integer"
            )
        if offset < 0:
            raise Nat64Error(
                "NAT64_INVALID_FRAGMENT", "fragment offset is negative"
            )
        if isinstance(payload, str):
            raise TypeError("fragment payload must be bytes-like")
        payload = bytes(payload)

        with self._lock:
            if mapping_id not in self._mappings:
                raise Nat64Error(
                    "NAT64_NO_SESSION", "fragment has no matching session"
                )
            fragments = self._fragments.setdefault(mapping_id, {})
            fragments[offset] = payload
            if more_fragments:
                return {
                    "buffered": True,
                    "offset": offset,
                    "buffered_size": sum(
                        len(part) for part in fragments.values()
                    ),
                }

            ordered = sorted(fragments.items())
            assembled = b"".join(part for _, part in ordered)
            total_size = len(assembled)
            self._fragments.pop(mapping_id, None)
            return {
                "translated": True,
                "offset": offset,
                "total_size": total_size,
                "payload": assembled,
            }

    def should_expire(self, protocol, idle_seconds):
        """Return True when the protocol-specific idle timeout is reached."""
        return idle_seconds >= self._timeout_for(protocol)

    def expire(self, now):
        """Expire aged mappings; return {"expired": int}."""
        with self._lock:
            self._last_now = max(self._last_now, now)
            expired_ids = []
            for mapping_id, mapping in list(self._mappings.items()):
                idle = max(0, now - mapping["last_seen"])
                if idle >= mapping["timeout"]:
                    expired_ids.append(mapping_id)
            for mapping_id in expired_ids:
                self._remove_mapping(mapping_id)
            return {"expired": len(expired_ids)}

    @staticmethod
    def _scope_token(scope):
        return hashlib.sha256(str(scope).encode("utf-8")).hexdigest()[:16]

    def synthesize_dns64(self, ipv4, scope="global"):
        """Synthesize a DNS64 address carrying its scope; return the address."""
        try:
            ipv4_value = int(ipaddress.IPv4Address(ipv4))
        except (ipaddress.AddressValueError, ValueError):
            raise ValueError("invalid IPv4 address: {!r}".format(ipv4))

        token = self._scope_token(scope)
        parts = [
            "64",
            "ff9b",
            token[0:4],
            token[4:8],
            token[8:12],
            token[12:16],
            "{:04x}".format((ipv4_value >> 16) & 0xffff),
            "{:04x}".format(ipv4_value & 0xffff),
        ]
        return str(ipaddress.IPv6Address(":".join(parts)))

    def dns64_usable(self, address, scope):
        """Return False when a synthesized address is used outside its scope."""
        try:
            parsed = ipaddress.IPv6Address(address)
        except (ipaddress.AddressValueError, ValueError):
            return False
        parts = parsed.exploded.split(":")
        if parts[0] != "0064" or parts[1].lower() != "ff9b":
            return False
        encoded_scope = "".join(parts[2:6])
        return encoded_scope == self._scope_token(scope)

    def dump_state(self):
        """Serialize mappings, ports and remaining lifetimes."""
        with self._lock:
            saved_at = self._last_now
            mappings = []
            for mapping in sorted(
                    self._mappings.values(),
                    key=lambda item: (item["created_at"], item["mapping_id"])):
                serialized = dict(mapping)
                idle = max(0, saved_at - mapping["last_seen"])
                serialized["remaining_ttl"] = max(
                    0, int(mapping["timeout"] - idle)
                )
                mappings.append(serialized)
            inbound_ports = sorted(
                mapping["inbound_port"]
                for mapping in self._mappings.values()
                if mapping.get("inbound_port") is not None
            )
            return {
                "version": 1,
                "public_address": self.public_address,
                "internal_prefix": self.internal_prefix,
                "port_min": self.port_min,
                "port_max": self.port_max,
                "tcp_timeout": self.tcp_timeout,
                "udp_timeout": self.udp_timeout,
                "icmp_timeout": self.icmp_timeout,
                "saved_at": saved_at,
                "next_mapping": self._next_mapping,
                "next_port": self._next_port,
                "ports": sorted(self._allocated_ports),
                "inbound_ports": inbound_ports,
                "mappings": mappings,
            }

    def load_state(self, state):
        """Restore mappings and their remaining lifetime after a restart."""
        if isinstance(state, (str, bytes, bytearray)):
            state = json.loads(state)
        if not isinstance(state, dict):
            raise ValueError("NAT64 state must be a mapping")

        with self._lock:
            if "public_address" in state:
                self.public_address = state["public_address"]
            if "internal_prefix" in state:
                self.internal_prefix = state["internal_prefix"]
                self._internal_network = self._parse_internal_network(
                    self.internal_prefix
                )
            if "port_min" in state:
                self.port_min = int(state["port_min"])
            if "port_max" in state:
                self.port_max = int(state["port_max"])
            for field in ("tcp_timeout", "udp_timeout", "icmp_timeout"):
                if field in state:
                    setattr(self, field, state[field])

            self._sessions = {}
            self._mappings = {}
            self._aliases = {}
            self._mapping_aliases = {}
            self._allocated_ports = set()
            self._fragments = {}

            for raw_mapping in state.get("mappings", []):
                mapping = dict(raw_mapping)
                mapping.pop("remaining_ttl", None)
                mapping["src_ip"] = str(mapping["src_ip"])
                mapping["src_port"] = self._port_number(mapping["src_port"])
                mapping["dst_ip"] = str(mapping["dst_ip"])
                mapping["dst_port"] = self._port_number(mapping["dst_port"])
                mapping["protocol"] = self._normalize_protocol(
                    mapping["protocol"]
                )
                mapping.setdefault(
                    "timeout", self._timeout_for(mapping["protocol"])
                )
                mapping.setdefault("created_at", mapping.get("last_seen", 0))
                mapping.setdefault("last_seen", mapping["created_at"])
                mapping.setdefault(
                    "expires_at",
                    mapping["last_seen"] + mapping["timeout"],
                )
                mapping.setdefault("hairpin", False)
                mapping.setdefault("translated", mapping["hairpin"])
                mapping.setdefault("inbound_port", None)
                mapping.setdefault("xlat464", False)

                mapping_id = mapping["mapping_id"]
                self._mappings[mapping_id] = mapping
                self._sessions[self._session_key(
                    mapping["src_ip"], mapping["src_port"],
                    mapping["dst_ip"], mapping["dst_port"],
                    mapping["protocol"],
                )] = mapping_id
                self._index_mapping(mapping)
                self._allocated_ports.add(mapping["public_port"])
                if mapping.get("inbound_port") is not None:
                    self._allocated_ports.add(mapping["inbound_port"])

            self._next_mapping = int(state.get(
                "next_mapping", self._next_mapping
            ))
            for mapping_id in self._mappings:
                try:
                    serial = int(str(mapping_id).rsplit("-", 1)[1])
                except (IndexError, TypeError, ValueError):
                    continue
                self._next_mapping = max(self._next_mapping, serial + 1)

            next_port = int(state.get("next_port", self.port_min))
            if next_port < self.port_min or next_port > self.port_max:
                next_port = self.port_min
            self._next_port = next_port
            self._last_now = state.get("saved_at", self._last_now)
