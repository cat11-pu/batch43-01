"""mTLS client identity mapping (skeleton)."""


class MtlsError(Exception):
    """Error carrying a stable mTLS mapping error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class MtlsClientMapping:
    """Maps client certificates to exactly one tenant."""

    def __init__(self, clock_skew=0):
        self.clock_skew = clock_skew
        self._tenant_patterns = {}
        self._serial_to_fingerprint = {}

    def register_tenant(self, tenant, pattern):
        """Register the allowed SAN/CN pattern for a tenant."""
        raise NotImplementedError

    def extract_identity(self, cert):
        """Return the first SAN DNS name, falling back to the subject CN."""
        raise NotImplementedError

    def identity_matches(self, pattern, identity):
        """Match an identity; a wildcard covers exactly one label."""
        raise NotImplementedError

    def map_certificate(self, cert, tenant, now=1000):
        """Validate and map a certificate to one tenant.

        No expiry grace: expired certs raise "MTLS_CERT_EXPIRED".  Incomplete
        chains raise "MTLS_CHAIN_INCOMPLETE"; revoked serials and OCSP timeouts
        fail closed with "MTLS_REVOKED"; a serial reused by a different
        certificate raises "MTLS_SERIAL_REUSE" and an alarm; one certificate
        mapped to a second tenant raises "MTLS_MAPPING_CONFLICT"; identity
        outside the tenant pattern raises "MTLS_IDENTITY_MISMATCH".
        Returns {"tenant": str, "identity": str, "serial": str}.
        """
        raise NotImplementedError

    def revoke(self, serial):
        """Revoke a certificate serial; returns {"revoked": True}."""
        raise NotImplementedError

    def authorize(self, identity, tenant):
        """Return whether this identity is allowed to act for the tenant."""
        raise NotImplementedError

    def upstream_identity(self, headers, cert):
        """Return the certificate identity, ignoring injected headers."""
        raise NotImplementedError

    def start_session(self, cert, tenant, now=1000):
        """Start a session bound to the certificate identity."""
        raise NotImplementedError

    def resume_session(self, session_id, cert, now=1000):
        """Resume a session only for the original certificate identity.

        The certificate is revalidated; a different identity raises
        MtlsError("MTLS_IDENTITY_MISMATCH").
        """
        raise NotImplementedError

    def alarms(self):
        """Return alarm codes raised so far."""
        raise NotImplementedError
