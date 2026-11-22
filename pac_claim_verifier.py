"""PAC claim and signature verification (skeleton)."""


class PacError(Exception):
    """Configuration failure raised by the verifier."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class PacClaimVerifier:
    """Validates PAC signatures, buffers and SID history before building tokens."""

    def __init__(
        self,
        trusted_forests=("corp.example",),
        max_age=3600.0,
        accepted_key_versions=(1, 2),
        require_service_sig=True,
    ):
        self.trusted_forests = tuple(trusted_forests)
        self.max_age = max_age
        self.accepted_key_versions = tuple(accepted_key_versions)
        self.require_service_sig = require_service_sig

    def verify(self, pac, now=0.0):
        raise NotImplementedError
