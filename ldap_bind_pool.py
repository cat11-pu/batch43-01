"""LDAP bind pool (skeleton)."""


class LdapError(Exception):
    """Error carrying a stable LDAP error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class LdapBindPool:
    """Pooled LDAP connections with per-bind authorization identity."""

    def __init__(self, pool_size=4,
                 referral_whitelist=("dc=corp,dc=local",),
                 tls_required=True,
                 sasl_mechanisms=("EXTERNAL", "GSSAPI")):
        self.pool_size = pool_size
        self.referral_whitelist = tuple(referral_whitelist)
        self.tls_required = tls_required
        self.sasl_mechanisms = tuple(sasl_mechanisms)

    def acquire(self):
        """Check out a pooled connection; returns {"handle": int}."""
        raise NotImplementedError

    def release(self, handle):
        """Return a connection to the pool and clear its identity."""
        raise NotImplementedError

    def connect(self, handle, starttls=True):
        """Set up the transport; returns {"connected": True, "tls": bool}."""
        raise NotImplementedError

    def bind(self, handle, dn, password):
        """Bind a user; empty credentials raise "LDAP_ANONYMOUS_DENIED".

        Returns {"bound_dn": str} with the normalized DN.  A locked account
        raises "LDAP_ACCOUNT_LOCKED" even on an already bound connection.
        """
        raise NotImplementedError

    def bind_service(self, handle, service_dn, password):
        """Bind the pool's service account (administrative identity)."""
        raise NotImplementedError

    def rebind(self, handle, dn, password):
        """Atomically replace the authorization identity.

        The previous identity and roles must not survive.  Emits an audit entry
        with the previous and new identity.  Returns {"bound_dn": str}.
        """
        raise NotImplementedError

    def current_identity(self, handle):
        """Return the normalized bound DN for a connection, or None."""
        raise NotImplementedError

    def has_role(self, handle, role):
        """Return whether the current identity carries a role."""
        raise NotImplementedError

    def lock_account(self, dn):
        """Lock an account; returns {"locked": True}."""
        raise NotImplementedError

    def is_locked(self, dn):
        """Return whether an account is locked."""
        raise NotImplementedError

    def follow_referral(self, handle, target_dn):
        """Follow a referral only inside the whitelist.

        Outside targets raise LdapError("LDAP_REFERRAL_BLOCKED").
        """
        raise NotImplementedError

    def search(self, handle, base, page_cookie=None, query_id="default"):
        """Run a paged search bound to one query.

        Reusing a cookie under another query id raises
        LdapError("LDAP_COOKIE_REPLAY").  Returns {"cookie": str|None,
        "results": list}.
        """
        raise NotImplementedError

    def sasl_bind(self, handle, mechanism):
        """SASL bind that refuses downgrade to SIMPLE.

        Unsupported mechanisms raise LdapError("LDAP_SASL_DOWNGRADE").
        """
        raise NotImplementedError

    def root_dse(self):
        """Return the public root DSE attributes only."""
        raise NotImplementedError

    def audit_log(self):
        """Return bind audit entries with previous and new identities."""
        raise NotImplementedError
