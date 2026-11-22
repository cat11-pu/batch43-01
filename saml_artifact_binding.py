"""SAML artifact binding profile (skeleton)."""


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
        self.allowed_relay_hosts = tuple(allowed_relay_hosts)

    def issue_request(self, sp_entity, relay_state=None):
        """Record an outstanding AuthnRequest.

        Returns {"request_id": str, "relay_state": str|None}.  Entity ids are
        compared case-insensitively.
        """
        raise NotImplementedError

    def create_artifact(self, assertion, sp_entity):
        """Create a one-time artifact bound to an SP.

        Returns {"artifact": str}.
        """
        raise NotImplementedError

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
        raise NotImplementedError

    def validate_relay(self, relay_state):
        """Allow only relative paths or registered callback hosts.

        External hosts raise SamlError("SAML_RELAY_INVALID").
        """
        raise NotImplementedError

    def accept_idp_assertion(self, assertion, signed=True,
                             audience="sp.example", now=1000):
        """Accept an IdP-initiated assertion once.

        A repeated assertion id raises SamlError("SAML_ASSERTION_REPLAY").
        """
        raise NotImplementedError

    def dump_state(self):
        """Serialize used assertion ids and pending requests."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore replay state after a restart."""
        raise NotImplementedError
