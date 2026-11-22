"""Pushed authorization request URI store (skeleton)."""


class ParError(Exception):
    """Authorization failure raised by the store."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class ParRequestUriStore:
    """Stores pushed authorization requests with client binding and single use."""

    def __init__(self, state_path=None, request_uri_ttl=90.0, require_pkce_s256=True):
        self.state_path = state_path
        self.request_uri_ttl = request_uri_ttl
        self.require_pkce_s256 = require_pkce_s256

    def push(self, request, signature_valid=True):
        raise NotImplementedError

    def authorize(self, request_uri, client_id, redirect_uri=None, now=0.0):
        raise NotImplementedError

    def exchange(self, code, client_id, method="S256", code_verifier=None):
        raise NotImplementedError

    def pending_count(self):
        raise NotImplementedError
