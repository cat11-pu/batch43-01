"""WebAuthn credential store (skeleton)."""


class WebAuthnError(Exception):
    """Error carrying a stable WebAuthn error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class WebAuthnCredentialStore:
    """Registers and verifies WebAuthn credentials."""

    def __init__(self, require_uv=True, challenge_ttl=60,
                 allowed_origin="https://login.example.com"):
        self.require_uv = require_uv
        self.challenge_ttl = challenge_ttl
        self.allowed_origin = allowed_origin

    def begin_challenge(self, now=0):
        """Issue a single-use challenge.

        Returns {"challenge": str, "issued_at": int}; every call returns a new
        value.
        """
        raise NotImplementedError

    def register(self, user, credential_id, public_key, rp_id_hash,
                 sign_count=0, uv=True, resident=False, backup_eligible=False,
                 attestation_self_signed=False, attestation_chain_valid=True):
        """Register a credential for exactly one user.

        A credential id owned by another user raises
        WebAuthnError("WEBAUTHN_CREDENTIAL_CONFLICT"); repeating the same
        registration is idempotent.  A self-signed attestation raises
        WebAuthnError("WEBAUTHN_ATTESTATION_INVALID").
        Returns {"registered": True, "credential_id": str, "count": int,
        "duplicate": bool}.
        """
        raise NotImplementedError

    def authenticate(self, credential_id, rp_id_hash, challenge, sign_count,
                     uv=True, origin="https://login.example.com", now=0):
        """Verify an assertion.

        Hard checks: single-use challenge (reuse or expiry gives
        "WEBAUTHN_CHALLENGE_REPLAY"), RP id hash match
        ("WEBAUTHN_RP_MISMATCH"), user-verification
        ("WEBAUTHN_UV_REQUIRED"), origin ("WEBAUTHN_ORIGIN_INVALID"), frozen
        credential ("WEBAUTHN_CREDENTIAL_FROZEN") and monotonic signature
        counter ("WEBAUTHN_CLONE_WARNING").  Unknown ids raise
        "WEBAUTHN_UNKNOWN_CREDENTIAL".
        Returns {"authenticated": True, "sign_count": int, "user": str}.
        """
        raise NotImplementedError

    def find_resident(self, user):
        """Return only this user's resident credential ids."""
        raise NotImplementedError

    def credential_info(self, credential_id):
        """Return {"user", "sign_count", "backup_eligible", "frozen"}."""
        raise NotImplementedError

    def freeze(self, credential_id):
        """Freeze a suspicious credential; returns {"frozen": True}."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize credentials, counters and used challenges."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore credentials after a restart."""
        raise NotImplementedError
