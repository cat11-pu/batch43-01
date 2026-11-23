"""BGP RPKI origin validator."""

import ipaddress
import threading


class BgpError(Exception):
    """Error carrying a stable RPKI validation code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BgpOriginValidator:
    """Validates announcements against ROIs and installs only valid routes."""

    def __init__(self):
        self._generation = 0
        self._lock = threading.RLock()
        self._rois = []
        self._routes = {}
        self._states = {}

    @staticmethod
    def _check_record(record):
        if not isinstance(record, dict):
            raise BgpError("BGP_ROV_LOAD_FAILED", "record is not a mapping")
        for field in ("prefix", "max_length", "origin_as", "updated_at"):
            if field not in record:
                raise BgpError("BGP_ROV_LOAD_FAILED", "missing %s" % field)
        try:
            network = ipaddress.ip_network(record["prefix"], strict=False)
        except ValueError:
            raise BgpError("BGP_ROV_LOAD_FAILED", "bad prefix")
        max_length = record["max_length"]
        origin_as = record["origin_as"]
        updated_at = record["updated_at"]
        for value in (max_length, origin_as, updated_at):
            if isinstance(value, bool) or not isinstance(value, int):
                raise BgpError("BGP_ROV_LOAD_FAILED", "non-integer field")
        if not 0 <= max_length <= network.max_prefixlen:
            raise BgpError("BGP_ROV_LOAD_FAILED", "max_length out of range")
        return {
            "network": network,
            "max_length": max_length,
            "origin_as": origin_as,
            "updated_at": updated_at,
        }

    def load_rois(self, records):
        """Atomically replace the ROI table with ``records``.

        Each record is a dict with "prefix" (CIDR), "max_length" (int),
        "origin_as" (int) and "updated_at" (int).  A malformed batch raises
        BgpError("BGP_ROV_LOAD_FAILED") and leaves the previous table and
        generation untouched.  Returns {"loaded": int, "generation": int}.
        """
        try:
            parsed = [self._check_record(record) for record in records]
        except BgpError:
            raise
        except Exception:
            raise BgpError("BGP_ROV_LOAD_FAILED", "unreadable batch")
        with self._lock:
            self._rois = parsed
            self._generation += 1
            return {"loaded": len(parsed), "generation": self._generation}

    def snapshot(self):
        """Return {"generation": int, "count": int} for the current table."""
        with self._lock:
            return {"generation": self._generation, "count": len(self._rois)}

    @staticmethod
    def _invalid():
        return {"state": "invalid", "code": "BGP_ROV_INVALID"}

    def validate(self, prefix, origin_as, path=()):
        """Return {"state": "valid"|"invalid"|"notfound", "code": str|None}.

        Invalid covers: origin AS mismatch, announced length above the ROI
        max_length, any AS repeated in ``path``, wrong address family, and a
        non-scalar origin value.  "code" is "BGP_ROV_INVALID" when invalid.
        """
        try:
            network = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            return self._invalid()
        if isinstance(origin_as, bool) or not isinstance(origin_as, int):
            return self._invalid()
        hops = list(path)
        if len(set(hops)) != len(hops):
            return self._invalid()
        with self._lock:
            covering = [
                roi
                for roi in self._rois
                if roi["network"].version == network.version
                and network.subnet_of(roi["network"])
            ]
            if not covering:
                return {"state": "notfound", "code": None}
            roi = max(covering, key=lambda item: item["updated_at"])
        if roi["origin_as"] != origin_as:
            return self._invalid()
        if network.prefixlen > roi["max_length"]:
            return self._invalid()
        return {"state": "valid", "code": None}

    def install_route(self, prefix, origin_as, path=(), local_pref=100):
        """Install a route only when validation says valid.

        Returns {"installed": bool, "state": str, "code": str|None}.  An
        invalid route must never replace an already installed valid route,
        regardless of local preference.
        """
        result = self.validate(prefix, origin_as, path)
        state = result["state"]
        with self._lock:
            installed = False
            if state == "valid":
                self._routes[prefix] = {
                    "origin_as": origin_as,
                    "path": tuple(path),
                    "local_pref": local_pref,
                }
                self._states[prefix] = "valid"
                installed = True
            elif self._states.get(prefix) != "valid":
                self._states[prefix] = state if state == "invalid" else "unknown"
            return {"installed": installed, "state": state, "code": result["code"]}

    def withdraw(self, prefix):
        """Withdraw a prefix and clear every cached validation mark.

        Returns {"withdrawn": True, "state_cleared": True}.
        """
        with self._lock:
            self._routes.pop(prefix, None)
            self._states.pop(prefix, None)
            return {"withdrawn": True, "state_cleared": True}

    def rov_state(self, prefix):
        """Return the current validation state: valid, invalid or unknown."""
        with self._lock:
            return self._states.get(prefix, "unknown")

    def advertise(self):
        """Return the sorted list of prefixes safe to advertise."""
        with self._lock:
            prefixes = [
                prefix
                for prefix in self._routes
                if self._states.get(prefix) == "valid"
            ]
        return sorted(
            prefixes,
            key=lambda item: (
                ipaddress.ip_network(item, strict=False).version,
                int(ipaddress.ip_network(item, strict=False).network_address),
                ipaddress.ip_network(item, strict=False).prefixlen,
            ),
        )

    def soft_reconfigure(self, policies):
        """Rebuild the policy chain without dropping the ROV stage.

        Returns {"stages": list, "rov_enabled": True}.
        """
        stages = list(policies)
        if "rov" not in stages:
            stages.append("rov")
        return {"stages": stages, "rov_enabled": True}
