"""WebAuthn credential store."""

import base64
import secrets
import threading


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
        self._lock = threading.Lock()
        self._credentials = {}
        self._challenges = {}

    def begin_challenge(self, now=0):
        """Issue a single-use challenge.

        Returns {"challenge": str, "issued_at": int}; every call returns a new
        value.
        """
        challenge = secrets.token_hex(16)
        with self._lock:
            while challenge in self._challenges:
                challenge = secrets.token_hex(16)
            self._challenges[challenge] = {"issued_at": now, "used": False}
        return {"challenge": challenge, "issued_at": now}

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
        if attestation_self_signed or not attestation_chain_valid:
            raise WebAuthnError("WEBAUTHN_ATTESTATION_INVALID",
                                "attestation chain could not be validated")
        with self._lock:
            existing = self._credentials.get(credential_id)
            if existing is not None:
                if existing["user"] != user:
                    raise WebAuthnError(
                        "WEBAUTHN_CREDENTIAL_CONFLICT",
                        "credential id already registered to another user")
                return {"registered": True, "credential_id": credential_id,
                        "count": 1, "duplicate": True}
            self._credentials[credential_id] = {
                "user": user,
                "public_key": public_key,
                "rp_id_hash": rp_id_hash,
                "sign_count": sign_count,
                "resident": resident,
                "backup_eligible": backup_eligible,
                "frozen": False,
            }
        return {"registered": True, "credential_id": credential_id,
                "count": 1, "duplicate": False}

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
        with self._lock:
            record = self._credentials.get(credential_id)
            if record is None:
                raise WebAuthnError("WEBAUTHN_UNKNOWN_CREDENTIAL",
                                    "credential id is not registered")
            if record["frozen"]:
                raise WebAuthnError("WEBAUTHN_CREDENTIAL_FROZEN",
                                    "credential is frozen")
            state = self._challenges.get(challenge)
            if (state is None or state["used"]
                    or now - state["issued_at"] > self.challenge_ttl):
                raise WebAuthnError("WEBAUTHN_CHALLENGE_REPLAY",
                                    "challenge is unknown, used or expired")
            state["used"] = True
            if rp_id_hash != record["rp_id_hash"]:
                raise WebAuthnError("WEBAUTHN_RP_MISMATCH",
                                    "relying party id hash does not match")
            if self.require_uv and not uv:
                raise WebAuthnError("WEBAUTHN_UV_REQUIRED",
                                    "user verification is required")
            if origin != self.allowed_origin:
                raise WebAuthnError("WEBAUTHN_ORIGIN_INVALID",
                                    "origin is not allowed")
            stored = record["sign_count"]
            if sign_count <= stored and (sign_count != 0 or stored != 0):
                raise WebAuthnError(
                    "WEBAUTHN_CLONE_WARNING",
                    "signature counter regressed; authenticator may be cloned")
            record["sign_count"] = sign_count
            return {"authenticated": True, "sign_count": sign_count,
                    "user": record["user"]}

    def find_resident(self, user):
        """Return only this user's resident credential ids."""
        with self._lock:
            return [cid for cid, rec in self._credentials.items()
                    if rec["resident"] and rec["user"] == user]

    def credential_info(self, credential_id):
        """Return {"user", "sign_count", "backup_eligible", "frozen"}."""
        with self._lock:
            record = self._credentials.get(credential_id)
            if record is None:
                raise WebAuthnError("WEBAUTHN_UNKNOWN_CREDENTIAL",
                                    "credential id is not registered")
            return {"user": record["user"],
                    "sign_count": record["sign_count"],
                    "backup_eligible": record["backup_eligible"],
                    "frozen": record["frozen"]}

    def freeze(self, credential_id):
        """Freeze a suspicious credential; returns {"frozen": True}."""
        with self._lock:
            record = self._credentials.get(credential_id)
            if record is None:
                raise WebAuthnError("WEBAUTHN_UNKNOWN_CREDENTIAL",
                                    "credential id is not registered")
            record["frozen"] = True
        return {"frozen": True}

    @staticmethod
    def _encode_bytes(value):
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _decode_bytes(value):
        return base64.b64decode(value.encode("ascii"))

    def dump_state(self):
        """Serialize credentials, counters and used challenges."""
        with self._lock:
            return {
                "credentials": {
                    cid: {
                        "user": rec["user"],
                        "public_key": self._encode_bytes(rec["public_key"]),
                        "rp_id_hash": self._encode_bytes(rec["rp_id_hash"]),
                        "sign_count": rec["sign_count"],
                        "resident": rec["resident"],
                        "backup_eligible": rec["backup_eligible"],
                        "frozen": rec["frozen"],
                    }
                    for cid, rec in self._credentials.items()
                },
                "challenges": {
                    ch: dict(state) for ch, state in self._challenges.items()
                },
            }

    def load_state(self, state):
        """Restore credentials after a restart."""
        with self._lock:
            self._credentials = {
                cid: {
                    "user": rec["user"],
                    "public_key": self._decode_bytes(rec["public_key"]),
                    "rp_id_hash": self._decode_bytes(rec["rp_id_hash"]),
                    "sign_count": rec["sign_count"],
                    "resident": rec["resident"],
                    "backup_eligible": rec["backup_eligible"],
                    "frozen": rec["frozen"],
                }
                for cid, rec in state.get("credentials", {}).items()
            }
            self._challenges = {
                ch: dict(state_entry)
                for ch, state_entry in state.get("challenges", {}).items()
            }
