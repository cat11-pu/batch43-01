"""USB port authorization.

Decisions are made from the device descriptor (and every interface of a
composite device) before any driver binding, so a device that does not match
an allow rule can never bind a kernel driver.  Rule sets are fully validated
and swapped atomically: an invalid rule file never disables the last known
good policy.  All state is keyed by the physical port path so concurrent or
out-of-order events can never cross records, and every decision is audited
with its port path for replay.
"""

import threading


class UsbError(Exception):
    """Error carrying a stable USB policy error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class UsbPortAuthorizer:
    """Authorizes USB devices per interface before any driver binds."""

    def __init__(self):
        self._generation = 0
        self._rules = ()
        self._ports = {}
        self._audit = []
        self._sequence = 0
        self._suspended = False
        self._lock = threading.RLock()

    _ALLOWED_FIELDS = frozenset(("id", "vid", "pid", "serial", "class"))

    def load_rules(self, rules):
        """Atomically replace the rule set.

        Rules need id, vid and pid; a serial-only rule raises
        UsbError("USB_RULE_INVALID") and keeps the previous policy.  Returns
        {"loaded": int, "generation": int}.
        """
        if not isinstance(rules, (list, tuple)):
            raise UsbError("USB_RULE_INVALID", "rules must be a list")
        validated = []
        seen_ids = set()
        for rule in rules:
            self._validate_rule(rule, seen_ids)
            validated.append(dict(rule))
        with self._lock:
            self._rules = tuple(validated)
            self._generation += 1
            return {"loaded": len(validated), "generation": self._generation}

    @staticmethod
    def _validate_rule(rule, seen_ids):
        if not isinstance(rule, dict):
            raise UsbError("USB_RULE_INVALID", "rule must be an object")
        unknown = set(rule) - UsbPortAuthorizer._ALLOWED_FIELDS
        if unknown:
            raise UsbError(
                "USB_RULE_INVALID", "unknown field(s): %s" % sorted(unknown)
            )
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise UsbError("USB_RULE_INVALID", "rule id must be a string")
        if rule_id in seen_ids:
            raise UsbError("USB_RULE_INVALID", "duplicate rule id: %s" % rule_id)
        for field in ("vid", "pid"):
            value = rule.get(field)
            if not isinstance(value, int) or isinstance(value, bool):
                raise UsbError("USB_RULE_INVALID", "missing %s" % field)
            if not 0 <= value <= 0xFFFF:
                raise UsbError("USB_RULE_INVALID", "%s out of range" % field)
        if "serial" in rule and not isinstance(rule["serial"], str):
            raise UsbError("USB_RULE_INVALID", "serial must be a string")
        if "class" in rule and not isinstance(rule["class"], str):
            raise UsbError("USB_RULE_INVALID", "class must be a string")
        seen_ids.add(rule_id)

    def _match_rule(self, descriptor):
        for rule in self._rules:
            if rule["vid"] != descriptor.get("vid"):
                continue
            if rule["pid"] != descriptor.get("pid"):
                continue
            if "serial" in rule and rule["serial"] != descriptor.get("serial"):
                continue
            if "class" in rule and rule["class"] != descriptor.get("class"):
                continue
            return rule
        return None

    def _evaluate(self, device):
        interfaces = device.get("interfaces") or ()
        if interfaces:
            first_rule_id = None
            for interface in interfaces:
                rule = self._match_rule(interface)
                if rule is None:
                    return False, None
                if first_rule_id is None:
                    first_rule_id = rule["id"]
            return True, first_rule_id
        rule = self._match_rule(device)
        if rule is None:
            return False, None
        return True, rule["id"]

    def authorize(self, port_path, device):
        """Decide a device before driver binding.

        ``device`` is None until descriptors are read and raises
        UsbError("USB_DESCRIPTOR_REQUIRED"); a released port raises
        UsbError("USB_PORT_RELEASED").  Composite devices need every interface
        allowed; one denied interface denies the whole device.  Returns
        {"authorized": bool, "driver_bound": bool, "rule_id": str|None}.
        """
        with self._lock:
            port = self._port_state(port_path)
            if port["released"]:
                raise UsbError("USB_PORT_RELEASED", port_path)
            if device is None:
                raise UsbError("USB_DESCRIPTOR_REQUIRED", port_path)
            if not isinstance(device, dict):
                raise UsbError("USB_DESCRIPTOR_REQUIRED", port_path)
            if port["decision"] is not None and port["device"] == device:
                return dict(port["decision"])

            authorized, rule_id = self._evaluate(device)
            decision = {
                "authorized": authorized,
                "driver_bound": authorized,
                "rule_id": rule_id,
            }
            port.update(
                present=True,
                released=False,
                authorized=authorized,
                driver_bound=authorized,
                rule_id=rule_id,
                device=device,
                decision=decision,
            )
            self._sequence += 1
            self._audit.append(
                {
                    "sequence": self._sequence,
                    "generation": self._generation,
                    "port_path": port_path,
                    "vid": device.get("vid"),
                    "pid": device.get("pid"),
                    "serial": device.get("serial"),
                    "authorized": authorized,
                    "driver_bound": authorized,
                    "rule_id": rule_id,
                }
            )
            return dict(decision)

    def release_port(self, port_path):
        """Mark a port object as released; returns {"released": True}."""
        with self._lock:
            port = self._port_state(port_path)
            port.update(
                released=True,
                present=False,
                authorized=False,
                driver_bound=False,
                rule_id=None,
                device=None,
                decision=None,
            )
            return {"released": True}

    def device_present(self, port_path):
        """Return whether a device is still attached to the port."""
        with self._lock:
            return bool(self._ports.get(port_path, {}).get("present", False))

    def is_authorized(self, port_path):
        """Return whether the port currently holds an authorized device."""
        with self._lock:
            return bool(self._ports.get(port_path, {}).get("authorized", False))

    def _port_state(self, port_path):
        state = self._ports.get(port_path)
        if state is None:
            state = {
                "present": False,
                "released": False,
                "authorized": False,
                "driver_bound": False,
                "rule_id": None,
                "device": None,
                "decision": None,
            }
            self._ports[port_path] = state
        return state

    def suspend(self):
        """Suspend authorization state for sleep."""
        with self._lock:
            self._suspended = True
            return {"suspended": True}

    def resume(self):
        """Resume and require reauthorization of every attached device.

        Returns {"reauthorize_ports": list, "cleared": int}.
        """
        with self._lock:
            reauthorize_ports = sorted(
                path
                for path, port in self._ports.items()
                if port["present"] and not port["released"]
            )
            cleared = 0
            for path in reauthorize_ports:
                port = self._ports[path]
                if port["authorized"]:
                    cleared += 1
                port.update(
                    authorized=False,
                    driver_bound=False,
                    rule_id=None,
                    decision=None,
                )
            self._suspended = False
            return {
                "reauthorize_ports": reauthorize_ports,
                "cleared": cleared,
            }

    def audit_log(self):
        """Return authorization records including the port path."""
        with self._lock:
            return [dict(entry) for entry in self._audit]
