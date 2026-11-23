"""VLAN access-boundary enforcement."""

import json
import os
import threading


class VlanError(Exception):
    """Configuration failure raised by the enforcer."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class VlanBoundaryEnforcer:
    """Blocks double tagging, DTP and address moves at access ports."""

    def __init__(self, state_path=None, freeze_window=2.0, default_max_mac=1):
        self.state_path = state_path
        self.freeze_window = freeze_window
        self.default_max_mac = default_max_mac
        self._lock = threading.RLock()
        self._ports = {}
        self._sticky = {}
        self._frozen = set()
        self._last_seen = {}
        self._freeze_done = {}
        self._counters = {"freeze_actions": 0, "mac_moves": 0}
        self._load_state()

    def _load_state(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        self._sticky.update({str(mac): int(port) for mac, port in data.get("sticky", {}).items()})
        self._frozen.update(int(port) for port in data.get("frozen", []))
        self._last_seen.update({str(mac): float(ts) for mac, ts in data.get("last_seen", {}).items()})
        self._freeze_done.update(
            {str(mac): float(ts) for mac, ts in data.get("freeze_done", {}).items()}
        )

    def _save_state(self):
        if not self.state_path:
            return
        data = {
            "sticky": self._sticky,
            "frozen": sorted(self._frozen),
            "last_seen": self._last_seen,
            "freeze_done": self._freeze_done,
        }
        tmp_path = self.state_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp_path, self.state_path)

    @staticmethod
    def _result(action, code, vlan):
        return {"action": action, "code": code, "vlan": vlan}

    def configure_access(self, port, vlan, native_vlan=None, max_mac=None, voice_vlan=None):
        with self._lock:
            self._ports[port] = {
                "vlan": vlan,
                "native_vlan": vlan if native_vlan is None else native_vlan,
                "max_mac": self.default_max_mac if max_mac is None else max_mac,
                "voice_vlan": voice_vlan,
                "mode": "access",
            }

    def frame(self, src_mac, vlan, port, tags=(), ethertype="data", now=0.0):
        with self._lock:
            cfg = self._ports.get(port)
            if cfg is None:
                return self._result("drop", "PORT_NOT_ACCESS", None)

            if ethertype == "dtp":
                return self._result("drop", "DTP_DENIED", None)
            if ethertype in ("gvrp", "mvrp"):
                return self._result("drop", "VLAN_DYNAMIC_DENIED", None)

            tags = tuple(tags or ())
            if len(tags) >= 2:
                return self._result("drop", "VLAN_DOUBLE_TAG", None)
            if len(tags) == 1 and tags[0] != 0:
                if tags[0] != cfg["native_vlan"]:
                    return self._result("drop", "VLAN_NATIVE_MISMATCH", None)
                effective_vlan = tags[0]
            else:
                effective_vlan = cfg["vlan"]

            if ethertype == "voice":
                if cfg["voice_vlan"] is None or effective_vlan != cfg["voice_vlan"]:
                    return self._result("drop", "VLAN_NOT_ALLOWED", effective_vlan)
            elif cfg["voice_vlan"] is not None and effective_vlan == cfg["voice_vlan"]:
                return self._result("drop", "VLAN_NOT_ALLOWED", effective_vlan)

            if port in self._frozen:
                return self._result("drop", "PORT_FROZEN", effective_vlan)

            bound = self._sticky.get(src_mac)
            if bound is not None and bound != port:
                self._counters["mac_moves"] += 1
                last = self._last_seen.get(src_mac)
                in_window = last is not None and (now - last) <= self.freeze_window
                froze_at = self._freeze_done.get(src_mac)
                already_froze = froze_at is not None and (now - froze_at) <= self.freeze_window
                if in_window and not already_froze:
                    self._freeze_done[src_mac] = now
                    self._frozen.add(port)
                    self._counters["freeze_actions"] += 1
                    self._save_state()
                    return self._result("freeze", "MAC_MOVE_FREEZE", effective_vlan)
                return self._result("drop", "MAC_MOVE_DENIED", effective_vlan)

            if bound is None:
                learned = sum(1 for p in self._sticky.values() if p == port)
                if learned >= cfg["max_mac"]:
                    return self._result("drop", "PORT_SECURITY_VIOLATION", effective_vlan)
                self._sticky[src_mac] = port
            self._last_seen[src_mac] = now
            self._save_state()
            return self._result("forward", None, effective_vlan)

    def is_frozen(self, port):
        with self._lock:
            return port in self._frozen

    def release(self, port):
        with self._lock:
            self._frozen.discard(port)
            self._save_state()

    def port_down(self, port):
        with self._lock:
            self._save_state()

    def sticky_table(self):
        with self._lock:
            return dict(self._sticky)

    def counters(self):
        with self._lock:
            return dict(self._counters)
