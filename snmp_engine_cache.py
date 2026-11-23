"""SNMPv3 engine cache and USM request checks."""

from __future__ import annotations

import copy
import hmac
import json
import math
import threading

_MAX_TIME_WINDOW = 150
_AUTH_FAILURE_DETAIL = "authentication failed"


class SnmpError(Exception):
    """Error carrying a USM report or local error code."""

    def __init__(self, code, detail="", data=None):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.data = data


class SnmpEngineCache:
    """Caches discovered engines and validates SNMPv3 requests."""

    _STATE_VERSION = 1

    def __init__(self, max_engines=8, time_window=150):
        self.max_engines = max_engines
        if isinstance(time_window, bool) or not isinstance(
                time_window, (int, float)):
            raise TypeError("time_window must be numeric")
        if time_window < 0:
            raise ValueError("time_window cannot be negative")
        self.time_window = min(time_window, _MAX_TIME_WINDOW)
        self._lock = threading.RLock()
        self._engines = {}
        self._responses = {}
        self._processed = 0
        self._cached_hits = 0

    @staticmethod
    def _cache_key(engine_id, user):
        return repr((engine_id, user))

    @staticmethod
    def _normalise_protocol(protocol):
        protocol = str(protocol or "").strip().upper().replace("_", "-")
        return {
            "SHA": "SHA-1",
            "SHA1": "SHA-1",
            "SHA-1": "SHA-1",
            "SHA256": "SHA-256",
            "SHA-256": "SHA-256",
            "SHA512": "SHA-512",
            "SHA-512": "SHA-512",
            "MD5": "MD5",
        }.get(protocol, protocol)

    @staticmethod
    def _matches_engine(entry, engine_id):
        return hmac.compare_digest(
            str(entry["engine_id"]).encode("utf-8"),
            str(engine_id).encode("utf-8"),
        )

    @staticmethod
    def _same_key_material(left, right):
        if left is None or right is None:
            return False
        if isinstance(left, str):
            left = left.encode("utf-8")
        if isinstance(right, str):
            right = right.encode("utf-8")
        try:
            left = bytes(left)
            right = bytes(right)
        except (TypeError, ValueError):
            return left == right
        return hmac.compare_digest(left, right)

    @staticmethod
    def _as_number(value):
        if isinstance(value, bool):
            raise TypeError("boolean is not a timestamp")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("timestamp must be finite")
        return number

    def _raise_not_in_time_window(self):
        raise SnmpError("usmStatsNotInTimeWindows", data=None)

    def _is_timely(self, entry, request):
        if "engine_boots" in request:
            try:
                if int(request["engine_boots"]) != int(entry["engine_boots"]):
                    return False
            except (TypeError, ValueError, OverflowError):
                return False

        try:
            request_time = self._as_number(request["engine_time"])
            engine_time = self._as_number(entry["engine_time"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        return abs(request_time - engine_time) <= self.time_window

    def _auth_failure(self):
        raise SnmpError("usmStatsAuthFailure", detail=_AUTH_FAILURE_DETAIL)

    def discover(self, engine_id, engine_boots, engine_time, user, source_ip,
                 auth_protocol="SHA-256", context=""):
        """Create or refresh a cache entry keyed by (engine_id, user).

        Returns {"cache_key": str, "created": bool, "cache_size": int}.
        Creating more than ``max_engines`` entries raises
        SnmpError("SNMP_ENGINE_CACHE_FULL"); discovering an existing key only
        refreshes boots/time.
        """
        key = (engine_id, user)
        with self._lock:
            entry = self._engines.get(key)
            if entry is None:
                if len(self._engines) >= self.max_engines:
                    raise SnmpError("SNMP_ENGINE_CACHE_FULL")
                entry = {
                    "engine_id": engine_id,
                    "engine_boots": int(engine_boots),
                    "engine_time": int(engine_time),
                    "user": user,
                    "source_ip": source_ip,
                    "auth_protocol": self._normalise_protocol(auth_protocol),
                    "context": context,
                }
                self._engines[key] = entry
                created = True
            else:
                boots = int(engine_boots)
                engine_clock = int(engine_time)
                boots_changed = int(entry["engine_boots"]) != boots
                entry["engine_boots"] = boots
                entry["engine_time"] = engine_clock
                if boots_changed:
                    self._discard_responses(engine_id, user)
                else:
                    self._prune_responses(engine_id, user, engine_clock)
                created = False

            return {
                "cache_key": self._cache_key(engine_id, user),
                "created": created,
                "cache_size": len(self._engines),
            }

    def get_engine(self, engine_id, user):
        """Return {"engine_boots": int, "engine_time": int, "user": str}."""
        with self._lock:
            entry = self._engines.get((engine_id, user))
            if entry is None:
                raise SnmpError("SNMP_UNKNOWN_ENGINE")
            return {
                "engine_boots": int(entry["engine_boots"]),
                "engine_time": int(entry["engine_time"]),
                "user": entry["user"],
            }

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
        with self._lock:
            engine_id = request.get("engine_id")
            user = request.get("user")
            entry = self._engines.get((engine_id, user))

            # Do not reveal whether an account exists.
            if entry is None:
                self._auth_failure()

            configured_protocol = self._normalise_protocol(
                entry["auth_protocol"])
            requested_protocol = self._normalise_protocol(
                request.get("auth_protocol"))
            if requested_protocol != configured_protocol:
                raise SnmpError("usmStatsWrongDigests")

            if not request.get("auth_ok", False):
                self._auth_failure()

            auth_key = request.get("auth_key")
            privacy_key = request.get("privacy_key")
            if self._same_key_material(auth_key, privacy_key):
                raise SnmpError("SNMP_KEY_REUSE")

            if not self._is_timely(entry, request):
                self._raise_not_in_time_window()

            if request.get("context", "") != entry["context"]:
                raise SnmpError("SNMP_CONTEXT_MISMATCH")

            request_id = request.get("request_id")
            response_key = (engine_id, user, request_id)
            cached = self._responses.get(response_key)
            if cached is not None:
                self._cached_hits += 1
                return copy.deepcopy(cached["response"])

            response = {
                "ok": True,
                "request_id": request_id,
                "data": copy.deepcopy(request.get("data")),
            }
            self._responses[response_key] = {
                "engine_time": self._as_number(request.get("engine_time")),
                "response": copy.deepcopy(response),
            }
            self._processed += 1
            return response

    def _discard_responses(self, engine_id, user):
        for key in [
                key for key in self._responses
                if key[0] == engine_id and key[1] == user]:
            del self._responses[key]

    def _prune_responses(self, engine_id, user, reference_time):
        stale = [
            key for key, record in self._responses.items()
            if key[0] == engine_id
            and key[1] == user
            and abs(record["engine_time"] - reference_time)
            > self.time_window
        ]
        for key in stale:
            del self._responses[key]

    def stats(self):
        """Return {"processed": int, "cached_hits": int}."""
        with self._lock:
            return {
                "processed": self._processed,
                "cached_hits": self._cached_hits,
            }

    def handle_report(self, report, authenticated):
        """Ignore unauthenticated reports.

        Returns {"ignored": bool}; unauthenticated input must yield True.
        """
        if not authenticated:
            return {"ignored": True}

        engine_id = report.get("engine_id")
        with self._lock:
            known = any(
                self._matches_engine(entry, engine_id)
                for entry in self._engines.values()
            )
        return {"ignored": not known}

    def accept_trap(self, trap):
        """Accept a v3 trap only for a known engine.

        Raise SnmpError("SNMP_UNKNOWN_ENGINE") for an unknown engine id.
        Returns {"accepted": True}.
        """
        engine_id = trap.get("engine_id")
        with self._lock:
            known = any(
                self._matches_engine(entry, engine_id)
                for entry in self._engines.values()
            )
        if not known:
            raise SnmpError("SNMP_UNKNOWN_ENGINE")
        return {"accepted": True}

    def dump_state(self):
        """Serialize engine boots/time and cache entries."""
        with self._lock:
            engines = [
                {
                    "engine_id": entry["engine_id"],
                    "engine_boots": int(entry["engine_boots"]),
                    "engine_time": int(entry["engine_time"]),
                    "user": entry["user"],
                    "source_ip": entry["source_ip"],
                    "auth_protocol": entry["auth_protocol"],
                    "context": entry["context"],
                    "cache_key": self._cache_key(
                        entry["engine_id"], entry["user"]),
                }
                for entry in self._engines.values()
            ]
            responses = [
                {
                    "engine_id": key[0],
                    "user": key[1],
                    "request_id": key[2],
                    "engine_time": record["engine_time"],
                    "response": copy.deepcopy(record["response"]),
                }
                for key, record in self._responses.items()
            ]
            return {
                "version": self._STATE_VERSION,
                "engines": engines,
                "responses": responses,
                "stats": self.stats(),
            }

    @staticmethod
    def _decode_state(state):
        if isinstance(state, (str, bytes, bytearray)):
            state = json.loads(state)
        if not isinstance(state, dict):
            raise TypeError("state must be a mapping or JSON object")
        return state

    @staticmethod
    def _engine_rows(state):
        rows = state.get("engines", state.get("cache", []))
        if isinstance(rows, dict):
            values = list(rows.values())
            if values and all(
                    isinstance(value, dict)
                    and "engine_id" in value
                    and "user" in value
                    for value in values):
                return values

            flattened = []
            for engine_id, users in rows.items():
                if not isinstance(users, dict):
                    continue
                if "engine_boots" in users and "engine_time" in users:
                    row = dict(users)
                    row.setdefault("engine_id", engine_id)
                    row.setdefault("user", row.get("user"))
                    flattened.append(row)
                    continue
                for user, record in users.items():
                    if not isinstance(record, dict):
                        continue
                    row = dict(record)
                    row.setdefault("engine_id", engine_id)
                    row.setdefault("user", user)
                    flattened.append(row)
            return flattened
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []

    def load_state(self, state):
        """Restore engine state after a restart."""
        state = self._decode_state(state)
        engines = {}
        for row in self._engine_rows(state):
            if "engine_id" not in row or "user" not in row:
                continue
            key = (row["engine_id"], row["user"])
            engines[key] = {
                "engine_id": row["engine_id"],
                "engine_boots": int(row["engine_boots"]),
                "engine_time": int(row["engine_time"]),
                "user": row["user"],
                "source_ip": row.get("source_ip"),
                "auth_protocol": self._normalise_protocol(
                    row.get("auth_protocol", "SHA-256")),
                "context": row.get("context", ""),
            }

        if len(engines) > self.max_engines:
            raise SnmpError("SNMP_ENGINE_CACHE_FULL")

        responses = {}
        for row in state.get("responses", []):
            if not isinstance(row, dict):
                continue
            try:
                key = (row["engine_id"], row["user"], row["request_id"])
                timestamp = self._as_number(row["engine_time"])
                response = row["response"]
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if not isinstance(response, dict):
                continue
            responses[key] = {
                "engine_time": timestamp,
                "response": copy.deepcopy(response),
            }

        with self._lock:
            self._engines = engines
            self._responses = responses
            stats = state.get("stats", {})
            self._processed = int(stats.get("processed", 0))
            self._cached_hits = int(stats.get("cached_hits", 0))
