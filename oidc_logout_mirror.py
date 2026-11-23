"""OIDC logout mirror."""

import uuid


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
        self._sessions = {}
        self._refresh_tokens = {}
        self._logout_tokens = {}
        self._callbacks = {}
        self._deliveries = {}

    def create_session(self, user, device):
        """Create a login session; returns {"session_id", "user", "device"}."""
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = {
            "user": user,
            "device": device,
            "active": True,
            "logout_token": None,
            "issued_at": None,
        }
        return {"session_id": session_id, "user": user, "device": device}

    def logout(self, session_id, now=0):
        """Log out exactly one session.

        Returns {"token": str, "status": "logged_out"|"already_logged_out",
        "issued_at": int}.  Repeating the call returns the same token and never
        touches another session of the same user.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise OidcError("OIDC_INVALID_SESSION", "unknown session")
        if session["logout_token"] is not None:
            return {
                "token": session["logout_token"],
                "status": "already_logged_out",
                "issued_at": session["issued_at"],
            }
        token = uuid.uuid4().hex
        session["active"] = False
        session["logout_token"] = token
        session["issued_at"] = now
        self._logout_tokens[token] = {"session_id": session_id, "issued_at": now}
        return {"token": token, "status": "logged_out", "issued_at": now}

    def is_active(self, session_id):
        """Return True while the session may still be used."""
        session = self._sessions.get(session_id)
        return bool(session and session["active"])

    def issue_refresh_token(self, session_id):
        """Issue a refresh token for an active session."""
        if not self.is_active(session_id):
            raise OidcError("OIDC_INVALID_SESSION", "session is not active")
        token = uuid.uuid4().hex
        self._refresh_tokens[token] = {"session_id": session_id, "used": False}
        return {"refresh_token": token}

    def refresh(self, refresh_token, now=0):
        """Refresh with a single-use token.

        After logout, password change or reuse raise
        OidcError("OIDC_INVALID_SESSION"); logout must win over renewal.
        """
        record = self._refresh_tokens.get(refresh_token)
        if record is None or record["used"]:
            raise OidcError("OIDC_INVALID_SESSION",
                            "refresh token unknown or already used")
        if not self.is_active(record["session_id"]):
            raise OidcError("OIDC_INVALID_SESSION",
                            "session is no longer active")
        record["used"] = True
        return {"session_id": record["session_id"]}

    def register_callback(self, app, url):
        """Register the exact callback URL for a relying party."""
        self._callbacks[app] = url

    def deliver(self, token, app, callback_url=None, delivered=True):
        """Deliver a logout notification to one app.

        The callback URL must match the registered URL exactly (no prefix
        matching); otherwise raise OidcError("OIDC_CALLBACK_MISMATCH").
        Failed deliveries enter the retry queue.  Returns {"state": str,
        "attempts": int}.
        """
        registered = self._callbacks.get(app)
        url = callback_url if callback_url is not None else registered
        if registered is None or url != registered:
            raise OidcError("OIDC_CALLBACK_MISMATCH",
                            "callback URL does not match the registered URL")
        return self._record_delivery(token, app, delivered)

    def _record_delivery(self, token, app, delivered):
        key = (token, app)
        record = self._deliveries.get(key)
        if record is not None and record["state"] in ("delivered", "dead"):
            return dict(record)
        if record is None:
            record = {"state": "pending", "attempts": 0}
            self._deliveries[key] = record
        record["attempts"] += 1
        if delivered:
            record["state"] = "delivered"
        elif record["attempts"] >= self.max_retries:
            record["state"] = "dead"
        else:
            record["state"] = "pending"
        return dict(record)

    def record_front_channel(self, token, app, delivered):
        """Record front-channel iframe result.

        A blocked iframe must be pending, never delivered.
        """
        return self._record_delivery(token, app, delivered)

    def attempt_delivery(self, token, app, success):
        """Retry one queued delivery; dead after max_retries failures."""
        record = self._deliveries.get((token, app))
        if record is None:
            raise OidcError("OIDC_INVALID_TOKEN", "no such delivery")
        if record["state"] != "pending":
            return dict(record)
        record["attempts"] += 1
        if success:
            record["state"] = "delivered"
        elif record["attempts"] >= self.max_retries:
            record["state"] = "dead"
        return dict(record)

    def delivery_state(self, token, app):
        """Return {"state": str, "attempts": int} even for dead deliveries."""
        record = self._deliveries.get((token, app))
        if record is None:
            return {"state": "unknown", "attempts": 0}
        return dict(record)

    def retry_queue(self):
        """Return the pending delivery entries."""
        return [
            {"token": token, "app": app, "attempts": record["attempts"]}
            for (token, app), record in self._deliveries.items()
            if record["state"] == "pending"
        ]

    def change_password(self, user):
        """Invalidate every session and refresh token of a user.

        Returns {"invalidated_sessions": int}.
        """
        invalidated = 0
        for session in self._sessions.values():
            if session["user"] == user and session["active"]:
                session["active"] = False
                invalidated += 1
        for record in self._refresh_tokens.values():
            session = self._sessions.get(record["session_id"])
            if session is not None and session["user"] == user:
                record["used"] = True
        return {"invalidated_sessions": invalidated}

    def validate_token(self, token, now, skew=60):
        """Validate a logout token's issue time with clock skew.

        A token issued in the future beyond ``skew`` raises
        OidcError("OIDC_TOKEN_SKEW").
        """
        record = self._logout_tokens.get(token)
        if record is None:
            raise OidcError("OIDC_INVALID_TOKEN", "unknown logout token")
        if record["issued_at"] - now > skew:
            raise OidcError("OIDC_TOKEN_SKEW",
                            "token issue time is too far in the future")
        return {"valid": True}

    def dump_state(self):
        """Serialize sessions, tokens and delivery queues."""
        return {
            "max_retries": self.max_retries,
            "sessions": {sid: dict(s) for sid, s in self._sessions.items()},
            "refresh_tokens": {t: dict(r)
                               for t, r in self._refresh_tokens.items()},
            "logout_tokens": {t: dict(r)
                              for t, r in self._logout_tokens.items()},
            "callbacks": dict(self._callbacks),
            "deliveries": [
                {"token": token, "app": app, "state": record["state"],
                 "attempts": record["attempts"]}
                for (token, app), record in self._deliveries.items()
            ],
        }

    def load_state(self, state):
        """Restore state after a restart without losing pending deliveries."""
        self.max_retries = state.get("max_retries", self.max_retries)
        self._sessions = {sid: dict(s)
                          for sid, s in state.get("sessions", {}).items()}
        self._refresh_tokens = {t: dict(r) for t, r in
                                state.get("refresh_tokens", {}).items()}
        self._logout_tokens = {t: dict(r) for t, r in
                               state.get("logout_tokens", {}).items()}
        self._callbacks = dict(state.get("callbacks", {}))
        self._deliveries = {}
        for entry in state.get("deliveries", []):
            self._deliveries[(entry["token"], entry["app"])] = {
                "state": entry["state"],
                "attempts": entry["attempts"],
            }
