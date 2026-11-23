"""SAML artifact binding profile."""

import secrets
import threading
from urllib.parse import urlsplit


class SamlError(Exception):
    """Error carrying a stable SAML error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SamlArtifactBinding:
    """One-time SAML artifacts bound to an SP and an outstanding request."""

    def __init__(self, clock_skew=120,
                 allowed_relay_hosts=("portal.example.com",)):
        self.clock_skew = clock_skew
        self.allowed_relay_hosts = tuple(
            self._normalize(host) for host in allowed_relay_hosts)
        self._lock = threading.RLock()
        self._pending_requests = {}
        self._artifacts = {}
        self._used_artifacts = set()
        self._used_assertion_ids = {}
        self.audit_log = []

    @staticmethod
    def _normalize(entity_id):
        return str(entity_id).strip().lower()

    def _audit(self, action, **fields):
        entry = {"action": action}
        entry.update(fields)
        self.audit_log.append(entry)

    def issue_request(self, sp_entity, relay_state=None):
        """Record an outstanding AuthnRequest.

        Returns {"request_id": str, "relay_state": str|None}.  Entity ids are
        compared case-insensitively.
        """
        if relay_state is not None:
            self.validate_relay(relay_state)
        with self._lock:
            request_id = secrets.token_urlsafe(24)
            self._pending_requests[request_id] = {
                "sp_entity": self._normalize(sp_entity),
                "relay_state": relay_state,
            }
        return {"request_id": request_id, "relay_state": relay_state}

    def create_artifact(self, assertion, sp_entity):
        """Create a one-time artifact bound to an SP.

        Returns {"artifact": str}.
        """
        with self._lock:
            artifact = secrets.token_urlsafe(32)
            self._artifacts[artifact] = {
                "sp_entity": self._normalize(sp_entity),
                "assertion": assertion,
            }
        return {"artifact": artifact}

    def _check_assertion(self, assertion, sp_entity, now):
        if not assertion.get("assertion_signed"):
            raise SamlError("SAML_SIGNATURE_MISSING",
                            "assertion itself must be signed")
        audiences = [self._normalize(aud)
                     for aud in assertion.get("audience") or []]
        if self._normalize(sp_entity) not in audiences:
            raise SamlError("SAML_AUDIENCE_INVALID",
                            "assertion audience does not include this SP")
        not_before = assertion.get("not_before", 0)
        not_on_or_after = assertion.get("not_on_or_after", 0)
        if now < not_before - self.clock_skew:
            raise SamlError("SAML_ASSERTION_EXPIRED",
                            "assertion not yet valid")
        if now > not_on_or_after + self.clock_skew:
            raise SamlError("SAML_ASSERTION_EXPIRED",
                            "assertion expired beyond clock skew")
        assertion_id = assertion.get("assertion_id")
        self._prune_assertion_ids(now)
        if assertion_id in self._used_assertion_ids:
            raise SamlError("SAML_ASSERTION_REPLAY",
                            "assertion id already consumed")
        return assertion_id

    def _prune_assertion_ids(self, now):
        expired = [aid for aid, expiry in self._used_assertion_ids.items()
                   if expiry < now]
        for aid in expired:
            del self._used_assertion_ids[aid]

    def _consume_assertion(self, assertion, now):
        assertion_id = assertion.get("assertion_id")
        not_on_or_after = assertion.get("not_on_or_after", now)
        self._used_assertion_ids[assertion_id] = (
            not_on_or_after + self.clock_skew)
        return assertion_id

    def resolve_artifact(self, artifact, sp_entity, request_id, tls=True,
                         now=1000):
        """Resolve an artifact exactly once.

        Returns {"session_id": str, "relay_state": str|None,
        "assertion_id": str}.  Errors: SAML_ARTIFACT_REPLAY (used artifact),
        SAML_ARTIFACT_BINDING (wrong SP), SAML_REQUEST_UNKNOWN (missing or
        mismatched request id), SAML_SIGNATURE_MISSING (unsigned assertion),
        SAML_AUDIENCE_INVALID, SAML_ASSERTION_EXPIRED (beyond clock skew),
        SAML_ASSERTION_REPLAY and SAML_INSECURE_TRANSPORT.
        """
        if not tls:
            raise SamlError("SAML_INSECURE_TRANSPORT",
                            "artifact resolution requires TLS")
        with self._lock:
            record = self._artifacts.pop(artifact, None)
            if record is None:
                self._audit("resolve", artifact=artifact,
                            sp_entity=sp_entity, outcome="replay")
                raise SamlError("SAML_ARTIFACT_REPLAY",
                                "artifact unknown or already used")
            self._used_artifacts.add(artifact)
            sp = self._normalize(sp_entity)
            if record["sp_entity"] != sp:
                self._audit("resolve", artifact=artifact, sp_entity=sp,
                            outcome="binding-mismatch")
                raise SamlError("SAML_ARTIFACT_BINDING",
                                "artifact was issued to a different SP")
            pending = self._pending_requests.pop(request_id, None)
            if not request_id or pending is None or \
                    pending["sp_entity"] != sp:
                self._audit("resolve", artifact=artifact, sp_entity=sp,
                            request_id=request_id, outcome="request-unknown")
                raise SamlError("SAML_REQUEST_UNKNOWN",
                                "no outstanding request for this id")
            assertion = record["assertion"]
            assertion_id = self._check_assertion(assertion, sp, now)
            self._consume_assertion(assertion, now)
            session_id = secrets.token_urlsafe(24)
            self._audit("resolve", artifact=artifact, sp_entity=sp,
                        request_id=request_id, assertion_id=assertion_id,
                        session_id=session_id, outcome="success")
            return {"session_id": session_id,
                    "relay_state": pending["relay_state"],
                    "assertion_id": assertion_id}

    def validate_relay(self, relay_state):
        """Allow only relative paths or registered callback hosts.

        External hosts raise SamlError("SAML_RELAY_INVALID").
        """
        parsed = urlsplit(relay_state)
        if parsed.scheme or parsed.netloc:
            host = (parsed.hostname or "").lower()
            if parsed.scheme in ("http", "https") and \
                    host in self.allowed_relay_hosts:
                return {"valid": True}
            raise SamlError("SAML_RELAY_INVALID",
                            "relay state points at an unregistered host")
        if not relay_state.startswith("/") or relay_state.startswith("//"):
            raise SamlError("SAML_RELAY_INVALID",
                            "relay state must be a relative path")
        return {"valid": True}

    def accept_idp_assertion(self, assertion, signed=True,
                             audience="sp.example", now=1000):
        """Accept an IdP-initiated assertion once.

        A repeated assertion id raises SamlError("SAML_ASSERTION_REPLAY").
        """
        with self._lock:
            if not signed:
                raise SamlError("SAML_SIGNATURE_MISSING",
                                "IdP-initiated assertion must be signed")
            assertion_id = self._check_assertion(assertion, audience, now)
            self._consume_assertion(assertion, now)
            session_id = secrets.token_urlsafe(24)
            self._audit("idp-initiated", assertion_id=assertion_id,
                        session_id=session_id, outcome="success")
            return {"session_id": session_id, "assertion_id": assertion_id}

    def dump_state(self):
        """Serialize used assertion ids and pending requests."""
        with self._lock:
            return {
                "used_assertion_ids": dict(self._used_assertion_ids),
                "pending_requests": dict(self._pending_requests),
                "used_artifacts": sorted(self._used_artifacts),
            }

    def load_state(self, state):
        """Restore replay state after a restart."""
        with self._lock:
            self._used_assertion_ids = dict(
                state.get("used_assertion_ids") or {})
            self._pending_requests = dict(state.get("pending_requests") or {})
            self._used_artifacts = set(state.get("used_artifacts") or [])
