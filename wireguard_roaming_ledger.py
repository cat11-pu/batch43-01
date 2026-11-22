"""WireGuard roaming ledger (skeleton)."""


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

    def issue_cookie(self, source_address):
        """Issue a cookie bound to the source address; return the cookie."""
        raise NotImplementedError

    def verify_cookie(self, public_key, cookie, source_address):
        """Return {"valid": True} only for a cookie issued for this source.

        Raise WireGuardError("WG_COOKIE_INVALID") otherwise; a failed cookie
        must never continue the handshake.
        """
        raise NotImplementedError

    def begin_handshake(self, public_key, sender_index, timestamp=0,
                        source_address="0.0.0.0"):
        """Create or reuse a session for a handshake initiation.

        Returns {"session_id": str, "created": bool, "replay_counter": int}.
        Replaying the same (public_key, sender_index) must hit the existing
        session and must not reset the replay counter.  A sender index already
        owned by another public key raises WireGuardError("WG_SESSION_CONFLICT").
        """
        raise NotImplementedError

    def complete_handshake(self, public_key, psk=None):
        """Finish the handshake; returns {"established": True}.

        Raise WireGuardError("WG_PSK_MISMATCH") when the preset key is set and
        the supplied key differs.
        """
        raise NotImplementedError

    def receive_transport(self, public_key, sequence, payload):
        """Accept transport data only after the handshake completed.

        Raise WireGuardError("WG_HANDSHAKE_INCOMPLETE") before that.
        Returns {"accepted": True}.
        """
        raise NotImplementedError

    def update_endpoint(self, public_key, address, authenticated):
        """Update the roaming endpoint only from an authenticated packet.

        Returns {"endpoint": str, "updated": bool}; an unauthenticated packet
        leaves the old endpoint in place.
        """
        raise NotImplementedError

    def keepalive_targets(self):
        """Return the list of endpoints that keepalives must go to."""
        raise NotImplementedError

    def check_replay(self, public_key, sequence):
        """Return {"accepted": bool, "code": str|None}.

        A sequence below the highest seen value is rejected with code
        "WG_REPLAY"; the counter must never move backwards.
        """
        raise NotImplementedError

    def dump_state(self):
        """Serialize sessions, endpoints and replay counters."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore state after a process restart."""
        raise NotImplementedError

    def add_allowed_ip(self, public_key, prefix):
        """Add a cryptokey-routing prefix for a peer.

        Overlapping prefixes between peers raise
        WireGuardError("WG_ALLOWED_IPS_CONFLICT") instead of silently
        overriding the earlier peer.
        """
        raise NotImplementedError

    def lookup_peer(self, destination_ip):
        """Return the public key owning the most specific matching prefix."""
        raise NotImplementedError

    def delete_peer(self, public_key):
        """Delete a peer and stop its keepalive timer.

        Returns {"deleted": bool, "keepalive_active": bool}.
        """
        raise NotImplementedError

    def rekey_due(self, public_key, now):
        """Return True when the handshake age reached max_handshake_age.

        The comparison must keep working after a system clock jump.
        """
        raise NotImplementedError
