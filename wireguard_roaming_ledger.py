"""WireGuard roaming ledger."""

import hashlib
import hmac
import ipaddress
import os
import threading


class WireGuardError(Exception):
    """Error carrying a stable WireGuard-style code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class WireGuardRoamingLedger:
    """Tracks WireGuard sessions, endpoints and replay counters per peer."""

    def __init__(self, max_handshake_age=120, preset_psk=None):
        self.max_handshake_age = max_handshake_age
        self.preset_psk = preset_psk
        self._lock = threading.RLock()
        self._cookie_secret = os.urandom(32)
        # public_key -> session dict
        self._sessions = {}
        # (public_key, sender_index) -> public_key, for replay hits
        self._session_keys = {}
        # sender_index -> public_key, for conflict detection
        self._sender_owners = {}
        # public_key -> endpoint address
        self._endpoints = {}
        # public_key -> highest accepted sequence number
        self._replay_counters = {}
        # public_key -> handshake timestamp
        self._handshake_times = {}
        # public_key -> highest "now" ever observed (clock-jump guard)
        self._max_now = {}
        # public_key -> list of ip_network prefixes
        self._allowed_ips = {}

    def issue_cookie(self, source_address):
        """Issue a cookie bound to the source address; return the cookie."""
        return hmac.new(
            self._cookie_secret,
            str(source_address).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def verify_cookie(self, public_key, cookie, source_address):
        """Return {"valid": True} only for a cookie issued for this source.

        Raise WireGuardError("WG_COOKIE_INVALID") otherwise; a failed cookie
        must never continue the handshake.
        """
        expected = self.issue_cookie(source_address)
        if not isinstance(cookie, str) or not hmac.compare_digest(expected, cookie):
            raise WireGuardError("WG_COOKIE_INVALID",
                                 "cookie does not match source address")
        return {"valid": True}

    def begin_handshake(self, public_key, sender_index, timestamp=0,
                        source_address="0.0.0.0"):
        """Create or reuse a session for a handshake initiation.

        Returns {"session_id": str, "created": bool, "replay_counter": int}.
        Replaying the same (public_key, sender_index) must hit the existing
        session and must not reset the replay counter.  A sender index already
        owned by another public key raises WireGuardError("WG_SESSION_CONFLICT").
        """
        with self._lock:
            owner = self._sender_owners.get(sender_index)
            if owner is not None and owner != public_key:
                raise WireGuardError(
                    "WG_SESSION_CONFLICT",
                    "sender index %r already owned by another peer" % sender_index,
                )
            existing = self._sessions.get(public_key)
            if existing is not None and existing["sender_index"] == sender_index:
                return {
                    "session_id": existing["session_id"],
                    "created": False,
                    "replay_counter": self._replay_counters.get(public_key, 0),
                }
            session_id = "%s:%s" % (public_key, sender_index)
            self._sessions[public_key] = {
                "session_id": session_id,
                "sender_index": sender_index,
                "established": False,
            }
            self._sender_owners[sender_index] = public_key
            self._session_keys[(public_key, sender_index)] = public_key
            self._endpoints[public_key] = source_address
            self._handshake_times[public_key] = timestamp
            self._max_now[public_key] = max(
                timestamp, self._max_now.get(public_key, timestamp))
            self._replay_counters.setdefault(public_key, 0)
            return {
                "session_id": session_id,
                "created": True,
                "replay_counter": self._replay_counters[public_key],
            }

    def complete_handshake(self, public_key, psk=None):
        """Finish the handshake; returns {"established": True}.

        Raise WireGuardError("WG_PSK_MISMATCH") when the preset key is set and
        the supplied key differs.
        """
        with self._lock:
            if self.preset_psk is not None and psk != self.preset_psk:
                raise WireGuardError("WG_PSK_MISMATCH",
                                     "pre-shared key does not match")
            session = self._sessions.get(public_key)
            if session is None:
                raise WireGuardError("WG_HANDSHAKE_INCOMPLETE",
                                     "no handshake in progress for peer")
            session["established"] = True
            return {"established": True}

    def receive_transport(self, public_key, sequence, payload):
        """Accept transport data only after the handshake completed.

        Raise WireGuardError("WG_HANDSHAKE_INCOMPLETE") before that.
        Returns {"accepted": True}.
        """
        with self._lock:
            session = self._sessions.get(public_key)
            if session is None or not session["established"]:
                raise WireGuardError(
                    "WG_HANDSHAKE_INCOMPLETE",
                    "transport data received before handshake completed",
                )
            return {"accepted": True}

    def update_endpoint(self, public_key, address, authenticated):
        """Update the roaming endpoint only from an authenticated packet.

        Returns {"endpoint": str, "updated": bool}; an unauthenticated packet
        leaves the old endpoint in place.
        """
        with self._lock:
            if not authenticated:
                return {
                    "endpoint": self._endpoints.get(public_key, "0.0.0.0"),
                    "updated": False,
                }
            self._endpoints[public_key] = address
            return {"endpoint": address, "updated": True}

    def keepalive_targets(self):
        """Return the list of endpoints that keepalives must go to."""
        with self._lock:
            return [
                self._endpoints[pk]
                for pk, session in self._sessions.items()
                if session["established"] and pk in self._endpoints
            ]

    def check_replay(self, public_key, sequence):
        """Return {"accepted": bool, "code": str|None}.

        A sequence below the highest seen value is rejected with code
        "WG_REPLAY"; the counter must never move backwards.
        """
        with self._lock:
            highest = self._replay_counters.get(public_key, 0)
            if sequence <= highest and highest > 0:
                return {"accepted": False, "code": "WG_REPLAY"}
            if sequence > highest:
                self._replay_counters[public_key] = sequence
            return {"accepted": True, "code": None}

    def dump_state(self):
        """Serialize sessions, endpoints and replay counters."""
        with self._lock:
            return {
                "sessions": {
                    pk: dict(session)
                    for pk, session in self._sessions.items()
                },
                "endpoints": dict(self._endpoints),
                "replay_counters": dict(self._replay_counters),
                "handshake_times": dict(self._handshake_times),
                "max_now": dict(self._max_now),
            }

    def load_state(self, state):
        """Restore state after a process restart."""
        with self._lock:
            sessions = state.get("sessions", {})
            self._sessions = {pk: dict(s) for pk, s in sessions.items()}
            self._sender_owners = {
                s["sender_index"]: pk for pk, s in self._sessions.items()
            }
            self._session_keys = {
                (pk, s["sender_index"]): pk
                for pk, s in self._sessions.items()
            }
            self._endpoints = dict(state.get("endpoints", {}))
            self._replay_counters = dict(state.get("replay_counters", {}))
            self._handshake_times = dict(state.get("handshake_times", {}))
            self._max_now = dict(state.get("max_now", {}))

    def add_allowed_ip(self, public_key, prefix):
        """Add a cryptokey-routing prefix for a peer.

        Overlapping prefixes between peers raise
        WireGuardError("WG_ALLOWED_IPS_CONFLICT") instead of silently
        overriding the earlier peer.
        """
        network = ipaddress.ip_network(prefix, strict=False)
        with self._lock:
            for owner, networks in self._allowed_ips.items():
                if owner == public_key:
                    continue
                for existing in networks:
                    if network.overlaps(existing):
                        raise WireGuardError(
                            "WG_ALLOWED_IPS_CONFLICT",
                            "%s overlaps %s owned by another peer"
                            % (network, existing),
                        )
            self._allowed_ips.setdefault(public_key, []).append(network)

    def lookup_peer(self, destination_ip):
        """Return the public key owning the most specific matching prefix."""
        address = ipaddress.ip_address(destination_ip)
        with self._lock:
            best_key = None
            best_prefixlen = -1
            for public_key, networks in self._allowed_ips.items():
                for network in networks:
                    if address in network and network.prefixlen > best_prefixlen:
                        best_key = public_key
                        best_prefixlen = network.prefixlen
            return best_key

    def delete_peer(self, public_key):
        """Delete a peer and stop its keepalive timer.

        Returns {"deleted": bool, "keepalive_active": bool}.
        """
        with self._lock:
            session = self._sessions.pop(public_key, None)
            if session is None:
                return {"deleted": False, "keepalive_active": False}
            self._sender_owners.pop(session["sender_index"], None)
            self._session_keys.pop(
                (public_key, session["sender_index"]), None)
            self._endpoints.pop(public_key, None)
            self._replay_counters.pop(public_key, None)
            self._handshake_times.pop(public_key, None)
            self._max_now.pop(public_key, None)
            self._allowed_ips.pop(public_key, None)
            return {"deleted": True, "keepalive_active": False}

    def rekey_due(self, public_key, now):
        """Return True when the handshake age reached max_handshake_age.

        The comparison must keep working after a system clock jump.
        """
        with self._lock:
            started = self._handshake_times.get(public_key)
            if started is None:
                return False
            effective_now = max(now, self._max_now.get(public_key, now))
            self._max_now[public_key] = effective_now
            return effective_now - started >= self.max_handshake_age
