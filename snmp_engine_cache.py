"""SNMPv3 engine cache and USM request checks (skeleton)."""


class SnmpError(Exception):
    """Error carrying a USM report or local error code."""

    def __init__(self, code, detail="", data=None):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.data = data


class SnmpEngineCache:
    """Caches discovered engines and validates SNMPv3 requests."""

    def __init__(self, max_engines=8, time_window=150):
        self.max_engines = max_engines
        self.time_window = time_window

    def discover(self, engine_id, engine_boots, engine_time, user, source_ip,
                 auth_protocol="SHA-256", context=""):
        """Create or refresh a cache entry keyed by (engine_id, user).

        Returns {"cache_key": str, "created": bool, "cache_size": int}.
        Creating more than ``max_engines`` entries raises
        SnmpError("SNMP_ENGINE_CACHE_FULL"); discovering an existing key only
        refreshes boots/time.
        """
        raise NotImplementedError

    def get_engine(self, engine_id, user):
        """Return {"engine_boots": int, "engine_time": int, "user": str}."""
        raise NotImplementedError

    def check_request(self, request):
        """Validate one SNMPv3 request and return its response.

        ``request`` has keys: engine_id, user, auth_protocol, auth_ok,
        auth_key, privacy_key, engine_time, request_id, context, data.
        Returns {"ok": True, "request_id": int, "data": dict}.

        Hard behaviors:
        - outside the time window raise SnmpError("usmStatsNotInTimeWindows")
          with ``data`` left as None (no data may leak);
        - unknown user and failed authentication raise the same code
          "usmStatsAuthFailure";
        - a duplicate request_id inside the window returns the cached response
          and is not processed a second time;
        - reused auth/privacy key material raises "SNMP_KEY_REUSE";
        - context mismatch raises "SNMP_CONTEXT_MISMATCH".
        """
        raise NotImplementedError

    def stats(self):
        """Return {"processed": int, "cached_hits": int}."""
        raise NotImplementedError

    def handle_report(self, report, authenticated):
        """Ignore unauthenticated reports.

        Returns {"ignored": bool}; unauthenticated input must yield True.
        """
        raise NotImplementedError

    def accept_trap(self, trap):
        """Accept a v3 trap only for a known engine.

        Raise SnmpError("SNMP_UNKNOWN_ENGINE") for an unknown engine id.
        Returns {"accepted": True}.
        """
        raise NotImplementedError

    def dump_state(self):
        """Serialize engine boots/time and cache entries."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore engine state after a restart."""
        raise NotImplementedError
