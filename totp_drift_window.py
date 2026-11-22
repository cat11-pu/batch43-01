"""TOTP drift window and replay store (skeleton)."""


class TotpError(Exception):
    """Error carrying a stable TOTP error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TotpDriftWindow:
    """Per-user TOTP secrets, drift and single-use counters."""

    def __init__(self, step=30, window=1, digits=6, max_failures=5):
        self.step = step
        self.window = window
        self.digits = digits
        self.max_failures = max_failures

    def enroll(self, user, secret, algorithm="SHA1", device=None):
        """Enroll a user secret.

        The same secret must not be shared by two users; otherwise raise
        TotpError("TOTP_SECRET_REUSE").  Returns {"enrolled": True,
        "secret_version": int}.
        """
        raise NotImplementedError

    def set_drift(self, user, seconds):
        """Record a per-user clock drift in seconds."""
        raise NotImplementedError

    def verify(self, user, code, timestamp, algorithm=None,
               source_ip="198.51.100.1"):
        """Verify a TOTP code with a small drift window.

        Returns {"ok": True, "counter": int, "last_counter": int}.  A code
        outside +/- ``window`` steps raises "TOTP_WINDOW"; a code at or below
        the last accepted counter is "TOTP_REPLAY"; a supplied algorithm that
        differs from enrollment is "TOTP_ALGORITHM_MISMATCH"; too many failures
        lock the account with "TOTP_LOCKED" regardless of source IP.
        """
        raise NotImplementedError

    def generate_backup_codes(self, user, count=3):
        """Generate one-time backup codes; returns {"codes": list}."""
        raise NotImplementedError

    def use_backup_code(self, user, code):
        """Consume a backup code once; reuse raises "TOTP_REPLAY"."""
        raise NotImplementedError

    def provisioning_uri(self, user):
        """Return the otpauth provisioning URI for a user."""
        raise NotImplementedError

    def access_log(self):
        """Return log lines; secrets must never appear here."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize last counters, secrets and failure counts."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore per-user counters after a restart."""
        raise NotImplementedError
