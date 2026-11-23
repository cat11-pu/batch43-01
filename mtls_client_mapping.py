"""mTLS client identity mapping (skeleton)."""

import itertools
import threading


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
        self._fingerprint_to_tenant = {}
        self._revoked_serials = set()
        self._sessions = {}
        self._session_counter = itertools.count(1)
        self._alarms = []
        self._lock = threading.Lock()

    def register_tenant(self, tenant, pattern):
        """Register the allowed SAN/CN pattern for a tenant."""
        self._tenant_patterns[tenant] = pattern

    def extract_identity(self, cert):
        """Return the first SAN DNS name, falling back to the subject CN."""
        san = cert.get("san") or []
        if san:
            return san[0]
        return cert.get("cn", "")

    def identity_matches(self, pattern, identity):
        """Match an identity; a wildcard covers exactly one label."""
        pattern = (pattern or "").lower()
        identity = (identity or "").lower()
        if not pattern or not identity:
            return False
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if not identity.endswith("." + suffix):
                return False
            prefix = identity[: -(len(suffix) + 1)]
            return prefix != "" and "." not in prefix
        return pattern == identity

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
        with self._lock:
            not_after = cert.get("not_after")
            if not_after is not None and now > not_after + self.clock_skew:
                raise MtlsError("MTLS_CERT_EXPIRED",
                                "certificate expired at %s" % not_after)

            if not cert.get("chain_complete", True):
                raise MtlsError("MTLS_CHAIN_INCOMPLETE",
                                "certificate chain could not be built")

            serial = cert.get("serial")
            if cert.get("revoked") or serial in self._revoked_serials:
                raise MtlsError("MTLS_REVOKED",
                                "certificate serial %s is revoked" % serial)

            if cert.get("ocsp_status", "good") != "good":
                raise MtlsError("MTLS_REVOKED",
                                "revocation status check failed closed")

            fingerprint = cert.get("fingerprint")
            known_fingerprint = self._serial_to_fingerprint.get(serial)
            if known_fingerprint is not None and known_fingerprint != fingerprint:
                self._alarms.append("MTLS_SERIAL_REUSE")
                raise MtlsError("MTLS_SERIAL_REUSE",
                                "serial %s reused by another certificate"
                                % serial)

            known_tenant = self._fingerprint_to_tenant.get(fingerprint)
            if known_tenant is not None and known_tenant != tenant:
                raise MtlsError("MTLS_MAPPING_CONFLICT",
                                "certificate already mapped to tenant %s"
                                % known_tenant)

            identity = self.extract_identity(cert)
            pattern = self._tenant_patterns.get(tenant)
            if pattern is None or not self.identity_matches(pattern, identity):
                raise MtlsError("MTLS_IDENTITY_MISMATCH",
                                "identity %r not allowed for tenant %s"
                                % (identity, tenant))

            self._serial_to_fingerprint[serial] = fingerprint
            self._fingerprint_to_tenant[fingerprint] = tenant
            return {"tenant": tenant, "identity": identity, "serial": serial}

    def revoke(self, serial):
        """Revoke a certificate serial; returns {"revoked": True}."""
        self._revoked_serials.add(serial)
        return {"revoked": True}

    def authorize(self, identity, tenant):
        """Return whether this identity is allowed to act for the tenant."""
        pattern = self._tenant_patterns.get(tenant)
        if pattern is None:
            return False
        return self.identity_matches(pattern, identity)

    def upstream_identity(self, headers, cert):
        """Return the certificate identity, ignoring injected headers."""
        return self.extract_identity(cert)

    def start_session(self, cert, tenant, now=1000):
        """Start a session bound to the certificate identity."""
        result = self.map_certificate(cert, tenant, now=now)
        session_id = "session-%d" % next(self._session_counter)
        session = {
            "session_id": session_id,
            "tenant": result["tenant"],
            "identity": result["identity"],
            "serial": result["serial"],
        }
        self._sessions[session_id] = session
        return session

    def resume_session(self, session_id, cert, now=1000):
        """Resume a session only for the original certificate identity.

        The certificate is revalidated; a different identity raises
        MtlsError("MTLS_IDENTITY_MISMATCH").
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise MtlsError("MTLS_SESSION_UNKNOWN",
                            "unknown session %r" % session_id)
        result = self.map_certificate(cert, session["tenant"], now=now)
        if result["identity"] != session["identity"]:
            raise MtlsError("MTLS_IDENTITY_MISMATCH",
                            "resumed certificate identity %r does not match "
                            "session identity %r"
                            % (result["identity"], session["identity"]))
        return {
            "session_id": session_id,
            "tenant": session["tenant"],
            "identity": session["identity"],
            "serial": session["serial"],
        }

    def alarms(self):
        """Return alarm codes raised so far."""
        return list(self._alarms)
