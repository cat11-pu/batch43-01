"""ARP / NDP sentinel for access-layer spoofing protection."""

import ipaddress
import json
import os
import tempfile
import threading


class SentinelError(Exception):
    """Configuration failure raised by the sentinel."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class ArpNdpSentinel:
    """Protects static and snooped bindings from ARP and NDP spoofing."""

    _COUNTER_NAMES = (
        "arp_binding_locked",
        "binding_locked",
        "ndp_binding_locked",
        "isolation_actions",
        "mac_moves",
        "address_conflicts",
        "conflicts_deduplicated",
        "ndp_router_flag_denied",
        "ndp_unspecified_source_ignored",
        "off_subnet_ignored",
        "port_isolated_blocks",
        "binding_table_evictions",
        "binding_table_full",
        "bindings_learned",
        "port_releases",
    )

    _AUTHORITATIVE_KINDS = frozenset(("static", "snooping"))

    def __init__(
        self,
        state_path=None,
        isolation_seconds=60,
        isolation_window=1.0,
        max_bindings=4096,
        max_dynamic_bindings=None,
    ):
        self.state_path = os.fspath(state_path) if state_path is not None else None
        self.isolation_seconds = isolation_seconds
        self.isolation_window = float(isolation_window)
        self.max_bindings = int(max_bindings)
        self.max_dynamic_bindings = int(
            max_bindings if max_dynamic_bindings is None else max_dynamic_bindings
        )
        if self.max_dynamic_bindings < 0:
            raise ValueError("max_dynamic_bindings must not be negative")

        self._lock = threading.RLock()
        self._bindings = {}
        self._mac_ports = {}
        self._conflict_episodes = {}
        self._blocked_ports = {}
        self._allowlist = set()
        self._networks = {}
        self._counters = {name: 0 for name in self._COUNTER_NAMES}
        self._sequence = 0
        self._load_state()

    def add_static(self, vlan, ip, mac, port=None):
        vlan = self._normalize_vlan(vlan)
        ip = self._normalize_ip(ip)
        mac = self._normalize_mac(mac)
        port = self._normalize_port(port)

        with self._lock:
            key = (vlan, ip)
            old = self._bindings.get(key)
            if old is not None:
                self._forget_mac_port(vlan, old["mac"], old["port"], key)
            self._sequence += 1
            self._bindings[key] = {
                "mac": mac,
                "port": port,
                "kind": "static",
                "updated_at": 0.0,
                "sequence": self._sequence,
            }
            if port is not None:
                self._mac_ports[(vlan, mac)] = port
            self._conflict_episodes.pop(key, None)
            self._save_state()

    def add_snooping(self, vlan, ip, mac, port):
        vlan = self._normalize_vlan(vlan)
        ip = self._normalize_ip(ip)
        mac = self._normalize_mac(mac)
        port = self._normalize_port(port)

        with self._lock:
            key = (vlan, ip)
            old = self._bindings.get(key)
            if old is not None and old["kind"] == "static":
                self._counters["binding_locked"] += 1
                return False
            if old is not None:
                self._forget_mac_port(vlan, old["mac"], old["port"], key)
            self._sequence += 1
            self._bindings[key] = {
                "mac": mac,
                "port": port,
                "kind": "snooping",
                "updated_at": 0.0,
                "sequence": self._sequence,
            }
            if port is not None:
                self._mac_ports[(vlan, mac)] = port
            self._conflict_episodes.pop(key, None)
            self._save_state()
            return True

    def add_allowlist(self, mac):
        mac = self._normalize_mac(mac)
        with self._lock:
            self._allowlist.add(mac)
            self._save_state()

    def configure_subnet(self, vlan, cidr):
        vlan = self._normalize_vlan(vlan)
        try:
            network = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError as exc:
            raise SentinelError("INVALID_SUBNET", str(exc)) from exc

        with self._lock:
            self._networks[vlan] = str(network)
            self._save_state()

    def observe_arp(self, vlan, ip, mac, port, gratuitous=False, now=0.0):
        vlan = self._normalize_vlan(vlan)
        ip = self._normalize_ip(ip)
        mac = self._normalize_mac(mac)
        port = self._normalize_port(port)
        now = float(now)

        with self._lock:
            if self._is_blocked_locked(port):
                self._counters["port_isolated_blocks"] += 1
                return self._result("blocked", "PORT_ISOLATED")
            if self._is_off_subnet_locked(vlan, ip):
                self._counters["off_subnet_ignored"] += 1
                return self._result("ignore", "OFF_SUBNET")
            return self._observe_locked(
                vlan, ip, mac, port, "arp", gratuitous=gratuitous, now=now
            )

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
        vlan = self._normalize_vlan(vlan)
        ip = self._normalize_ip(ip)
        mac = self._normalize_mac(mac)
        port = self._normalize_port(port)
        now = float(now)

        with self._lock:
            if self._is_blocked_locked(port):
                self._counters["port_isolated_blocks"] += 1
                return self._result("blocked", "PORT_ISOLATED")
            if src_unspecified:
                self._counters["ndp_unspecified_source_ignored"] += 1
                return self._result("ignore", "NDP_SOURCE_UNSPECIFIED")
            if router_flag:
                self._counters["ndp_router_flag_denied"] += 1
                return self._result("drop", "NDP_ROUTER_FLAG_DENIED")
            if self._is_off_subnet_locked(vlan, ip):
                self._counters["off_subnet_ignored"] += 1
                return self._result("ignore", "OFF_SUBNET")
            return self._observe_locked(
                vlan,
                ip,
                mac,
                port,
                "ndp",
                override_flag=override_flag,
                now=now,
            )

    def is_blocked(self, port):
        port = self._normalize_port(port)
        with self._lock:
            return self._is_blocked_locked(port)

    def release_port(self, port):
        port = self._normalize_port(port)
        with self._lock:
            was_blocked = self._blocked_ports.pop(port, None) is not None
            removed = False

            for key, binding in list(self._bindings.items()):
                if binding["kind"] == "dynamic" and binding["port"] == port:
                    del self._bindings[key]
                    removed = True

            for key, mac_port in list(self._mac_ports.items()):
                if mac_port == port:
                    del self._mac_ports[key]
                    removed = True

            for key, episode in list(self._conflict_episodes.items()):
                if port in episode["ports"]:
                    del self._conflict_episodes[key]
                    removed = True

            if was_blocked:
                self._counters["port_releases"] += 1
            if was_blocked or removed:
                self._save_state()
            return was_blocked

    def table(self, vlan):
        vlan = self._normalize_vlan(vlan)
        with self._lock:
            return {
                ip: binding["mac"]
                for (binding_vlan, ip), binding in self._bindings.items()
                if binding_vlan == vlan
            }

    def counters(self):
        with self._lock:
            return dict(self._counters)

    def _observe_locked(
        self,
        vlan,
        ip,
        mac,
        port,
        protocol,
        gratuitous=False,
        override_flag=False,
        now=0.0,
    ):
        key = (vlan, ip)
        existing = self._bindings.get(key)
        self._sequence += 1

        if existing is not None and existing["mac"] != mac:
            if existing["kind"] in self._AUTHORITATIVE_KINDS:
                self._counters["arp_binding_locked"] += 1
                self._counters["binding_locked"] += 1
                if protocol == "ndp":
                    self._counters["ndp_binding_locked"] += 1
                self._record_conflict(key, vlan, ip, port, now)
                self._save_state()
                return self._result(
                    "blocked",
                    "ARP_BINDING_LOCKED",
                    vlan=vlan,
                    ip=ip,
                    mac=existing["mac"],
                )

            isolated = self._record_conflict(key, vlan, ip, port, now)
            self._save_state()
            return self._result(
                "isolate" if isolated else "blocked",
                "ADDRESS_CONFLICT" if isolated else "ADDRESS_CONFLICT_DEDUPLICATED",
                vlan=vlan,
                ip=ip,
            )

        if existing is not None and existing["mac"] == mac:
            if existing["port"] is not None and existing["port"] != port:
                self._counters["mac_moves"] += 1
                self._counters["binding_locked"] += 1
                self._mac_ports[(vlan, mac)] = port
                self._isolate_port(port, "MAC_MOVED", vlan, ip, now)
                self._save_state()
                return self._result("move", "MAC_MOVED", vlan=vlan, ip=ip)

            existing["updated_at"] = now
            existing["sequence"] = self._sequence
            if port is not None:
                self._mac_ports[(vlan, mac)] = port
            return self._result("accept", "OK", vlan=vlan, ip=ip)

        previous_port = self._mac_ports.get((vlan, mac))
        if previous_port is not None and previous_port != port:
            self._counters["mac_moves"] += 1
            self._mac_ports[(vlan, mac)] = port
            self._isolate_port(port, "MAC_MOVED", vlan, ip, now)
            self._save_state()
            return self._result("move", "MAC_MOVED", vlan=vlan, ip=ip)

        if not self._make_room_for_dynamic():
            self._counters["binding_table_full"] += 1
            return self._result("drop", "BINDING_TABLE_FULL", vlan=vlan, ip=ip)

        self._bindings[key] = {
            "mac": mac,
            "port": port,
            "kind": "dynamic",
            "updated_at": now,
            "sequence": self._sequence,
        }
        if port is not None:
            self._mac_ports[(vlan, mac)] = port
        self._counters["bindings_learned"] += 1
        result = self._result("accept", "OK", vlan=vlan, ip=ip)
        self._save_state()
        return result

    def _record_conflict(self, key, vlan, ip, port, now):
        self._counters["address_conflicts"] += 1
        episode = self._conflict_episodes.get(key)
        if (
            episode is not None
            and abs(now - episode["start"]) <= self.isolation_window
        ):
            episode["ports"].add(port)
            self._counters["conflicts_deduplicated"] += 1
            return False

        self._conflict_episodes[key] = {"start": now, "ports": {port}}
        return self._isolate_port(port, "ADDRESS_CONFLICT", vlan, ip, now)

    def _isolate_port(self, port, reason, vlan, ip, now):
        if port is None or self._is_blocked_locked(port):
            return False
        self._blocked_ports[port] = {
            "reason": reason,
            "vlan": vlan,
            "ip": ip,
            "at": now,
        }
        self._counters["isolation_actions"] += 1
        return True

    def _make_room_for_dynamic(self):
        dynamic = [
            (binding["sequence"], key)
            for key, binding in self._bindings.items()
            if binding["kind"] == "dynamic"
        ]
        if len(dynamic) < self.max_dynamic_bindings:
            return True
        if not dynamic:
            return False

        _, key = min(dynamic)
        binding = self._bindings.pop(key)
        self._counters["binding_table_evictions"] += 1
        self._forget_mac_port(key[0], binding["mac"], binding["port"], key)
        return True

    def _forget_mac_port(self, vlan, mac, port, removing_key):
        mac_key = (vlan, mac)
        if self._mac_ports.get(mac_key) != port:
            return
        for key, binding in self._bindings.items():
            if key == removing_key:
                continue
            if key[0] == vlan and binding["mac"] == mac and binding["port"] == port:
                return
        self._mac_ports.pop(mac_key, None)

    def _is_blocked_locked(self, port):
        return port in self._blocked_ports

    def _is_off_subnet_locked(self, vlan, ip):
        cidr = self._networks.get(vlan)
        if cidr is None:
            return False
        try:
            return ipaddress.ip_address(ip) not in ipaddress.ip_network(cidr)
        except ValueError:
            return False

    @staticmethod
    def _normalize_vlan(vlan):
        if isinstance(vlan, bool):
            return int(vlan)
        if isinstance(vlan, int):
            return vlan
        if isinstance(vlan, str):
            value = vlan.strip()
            if value.isdigit():
                return int(value)
            return value
        return vlan

    @staticmethod
    def _normalize_port(port):
        if port is None:
            return None
        if isinstance(port, bool):
            return int(port)
        if isinstance(port, int):
            return port
        if isinstance(port, str) and port.strip().isdigit():
            return int(port.strip())
        return port

    @staticmethod
    def _normalize_mac(mac):
        if mac is None:
            return ""
        return str(mac).strip().lower().replace("-", ":")

    @staticmethod
    def _normalize_ip(ip):
        value = str(ip).strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return value.lower()

    @staticmethod
    def _result(action, code, **details):
        result = {"action": action, "code": code}
        result.update(details)
        return result

    def _load_state(self):
        if self.state_path is None or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, ValueError) as exc:
            raise SentinelError("STATE_LOAD_FAILED", str(exc)) from exc

        if not isinstance(state, dict):
            raise SentinelError("STATE_LOAD_FAILED", "state root must be an object")

        try:
            for item in state.get("bindings", []):
                vlan = self._normalize_vlan(item["vlan"])
                ip = self._normalize_ip(item["ip"])
                mac = self._normalize_mac(item["mac"])
                port = self._normalize_port(item.get("port"))
                kind = item["kind"]
                if kind not in ("static", "snooping", "dynamic"):
                    continue
                self._sequence = max(self._sequence, int(item.get("sequence", 0)))
                key = (vlan, ip)
                self._bindings[key] = {
                    "mac": mac,
                    "port": port,
                    "kind": kind,
                    "updated_at": float(item.get("updated_at", 0.0)),
                    "sequence": int(item.get("sequence", 0)),
                }
                if port is not None:
                    self._mac_ports[(vlan, mac)] = port

            for item in state.get("blocked_ports", []):
                port = self._normalize_port(item["port"])
                self._blocked_ports[port] = {
                    "reason": item.get("reason", "ISOLATED"),
                    "vlan": self._normalize_vlan(item.get("vlan")),
                    "ip": self._normalize_ip(item.get("ip"))
                    if item.get("ip") is not None
                    else None,
                    "at": float(item.get("at", 0.0)),
                }

            for item in state.get("conflict_episodes", []):
                key = (
                    self._normalize_vlan(item["vlan"]),
                    self._normalize_ip(item["ip"]),
                )
                ports = {
                    self._normalize_port(port) for port in item.get("ports", [])
                }
                self._conflict_episodes[key] = {
                    "start": float(item.get("start", 0.0)),
                    "ports": ports,
                }

            for item in state.get("mac_ports", []):
                vlan = self._normalize_vlan(item["vlan"])
                mac = self._normalize_mac(item["mac"])
                self._mac_ports[(vlan, mac)] = self._normalize_port(item["port"])

            for mac in state.get("allowlist", []):
                self._allowlist.add(self._normalize_mac(mac))

            for vlan, cidr in state.get("networks", {}).items():
                self._networks[self._normalize_vlan(vlan)] = str(cidr)

            for name, value in state.get("counters", {}).items():
                self._counters[name] = int(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise SentinelError("STATE_LOAD_FAILED", str(exc)) from exc

    def _save_state(self):
        if self.state_path is None:
            return

        state = {
            "version": 1,
            "bindings": [
                {
                    "vlan": vlan,
                    "ip": ip,
                    "mac": binding["mac"],
                    "port": binding["port"],
                    "kind": binding["kind"],
                    "updated_at": binding["updated_at"],
                    "sequence": binding["sequence"],
                }
                for (vlan, ip), binding in self._bindings.items()
            ],
            "blocked_ports": [
                {"port": port, **details}
                for port, details in self._blocked_ports.items()
            ],
            "conflict_episodes": [
                {
                    "vlan": key[0],
                    "ip": key[1],
                    "start": episode["start"],
                    "ports": sorted(
                        episode["ports"], key=lambda value: str(value)
                    ),
                }
                for key, episode in self._conflict_episodes.items()
            ],
            "mac_ports": [
                {"vlan": vlan, "mac": mac, "port": port}
                for (vlan, mac), port in self._mac_ports.items()
            ],
            "allowlist": sorted(self._allowlist),
            "networks": {
                str(vlan): cidr for vlan, cidr in self._networks.items()
            },
            "counters": dict(self._counters),
        }

        directory = os.path.dirname(os.path.abspath(self.state_path))
        try:
            os.makedirs(directory, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".arp-ndp-sentinel-", dir=directory
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                    json.dump(state, state_file, sort_keys=True, separators=(",", ":"))
                    state_file.flush()
                    os.fsync(state_file.fileno())
                os.replace(temporary, self.state_path)
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise SentinelError("STATE_WRITE_FAILED", str(exc)) from exc
