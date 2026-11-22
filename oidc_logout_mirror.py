"""OIDC logout mirror (skeleton)."""


class OidcError(Exception):
    """Error carrying a stable OIDC logout error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class OidcLogoutMirror:
    """Single-logout token minting and mirror delivery per relying party."""

    def __init__(self, max_retries=3):
        self.max_retries = max_retries

    def create_session(self, user, device):
        """Create a login session; returns {"session_id", "user", "device"}."""
        raise NotImplementedError

    def logout(self, session_id, now=0):
        """Log out exactly one session.

        Returns {"token": str, "status": "logged_out"|"already_logged_out",
        "issued_at": int}.  Repeating the call returns the same token and never
        touches another session of the same user.
        """
        raise NotImplementedError

    def is_active(self, session_id):
        """Return True while the session may still be used."""
        raise NotImplementedError

    def issue_refresh_token(self, session_id):
        """Issue a refresh token for an active session."""
        raise NotImplementedError

    def refresh(self, refresh_token, now=0):
        """Refresh with a single-use token.

        After logout, password change or reuse raise
        OidcError("OIDC_INVALID_SESSION"); logout must win over renewal.
        """
        raise NotImplementedError

    def register_callback(self, app, url):
        """Register the exact callback URL for a relying party."""
        raise NotImplementedError

    def deliver(self, token, app, callback_url=None, delivered=True):
        """Deliver a logout notification to one app.

        The callback URL must match the registered URL exactly (no prefix
        matching); otherwise raise OidcError("OIDC_CALLBACK_MISMATCH").
        Failed deliveries enter the retry queue.  Returns {"state": str,
        "attempts": int}.
        """
        raise NotImplementedError

    def record_front_channel(self, token, app, delivered):
        """Record front-channel iframe result.

        A blocked iframe must be pending, never delivered.
        """
        raise NotImplementedError

    def attempt_delivery(self, token, app, success):
        """Retry one queued delivery; dead after max_retries failures."""
        raise NotImplementedError

    def delivery_state(self, token, app):
        """Return {"state": str, "attempts": int} even for dead deliveries."""
        raise NotImplementedError

    def retry_queue(self):
        """Return the pending delivery entries."""
        raise NotImplementedError

    def change_password(self, user):
        """Invalidate every session and refresh token of a user.

        Returns {"invalidated_sessions": int}.
        """
        raise NotImplementedError

    def validate_token(self, token, now, skew=60):
        """Validate a logout token's issue time with clock skew.

        A token issued in the future beyond ``skew`` raises
        OidcError("OIDC_TOKEN_SKEW").
        """
        raise NotImplementedError

    def dump_state(self):
        """Serialize sessions, tokens and delivery queues."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore state after a restart without losing pending deliveries."""
        raise NotImplementedError
