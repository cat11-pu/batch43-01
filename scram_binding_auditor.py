"""SCRAM channel-binding auditor (skeleton)."""


class ScramError(Exception):
    """Authentication failure raised by the auditor."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class ScramBindingAuditor:
    """Validates SCRAM-PLUS channel binding and final signatures."""

    def __init__(self, min_iterations=4096, require_channel_binding=True):
        self.min_iterations = min_iterations
        self.require_channel_binding = require_channel_binding

    def begin(
        self,
        connection_id,
        username,
        client_nonce,
        server_nonce,
        cert_chain=None,
        channel_binding=None,
        mechanism="SCRAM-SHA-256-PLUS",
        iterations=4096,
        salt=b"salt",
        authzid=None,
        stored_key_version=1,
        derived_key_version=1,
    ):
        raise NotImplementedError

    def finish(
        self,
        connection_id,
        client_final_nonce,
        server_signature_valid=True,
        channel_binding_used=True,
    ):
        raise NotImplementedError

    def is_authenticated(self, connection_id):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
