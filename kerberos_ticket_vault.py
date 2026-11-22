"""Kerberos ticket cache vault (skeleton)."""


class KerberosError(Exception):
    """Error carrying a Kerberos code and failing stage."""

    def __init__(self, code, detail="", stage=""):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.stage = stage


class KerberosTicketVault:
    """Local TGT cache, replay cache and service-ticket bookkeeping."""

    def __init__(self, clock_skew=300, max_renewable=86400):
        self.clock_skew = clock_skew
        self.max_renewable = max_renewable

    def store_tgt(self, principal, ticket, key_version=1):
        """Store a TGT; ticket has ticket_id, expires_at, renew_till, realm.

        Returns {"stored": True, "key_version": int}.
        """
        raise NotImplementedError

    def renew(self, principal, now, client_time=None):
        """Renew a TGT.

        Expired TGT raises KerberosError("KRB_AP_ERR_TKT_EXPIRED",
        stage="renew"); too old for renewal raises
        "KRB_AP_ERR_RENEW_EXPIRED"; a client_time outside ``clock_skew``
        raises "KRB_AP_ERR_SKEW".  Returns {"renewed": True, "ticket_id": str,
        "expires_at": int}.
        """
        raise NotImplementedError

    def check_keytab(self, principal, key_version):
        """Verify keytab version; mismatch raises "KRB_AP_ERR_BADKEYVER"."""
        raise NotImplementedError

    def authenticate(self, principal, authenticator_id):
        """Authenticate an authenticator exactly once.

        A repeated authenticator raises KerberosError("KRB_AP_ERR_REPLAY").
        """
        raise NotImplementedError

    def set_service_ticket(self, principal, service, ticket_id):
        """Cache a service ticket for a principal."""
        raise NotImplementedError

    def get_service_ticket(self, principal, service):
        """Return the cached service ticket or raise "KRB_NO_TICKET"."""
        raise NotImplementedError

    def switch_principal(self, principal):
        """Switch the active principal and clear the previous cache.

        Returns {"current": str, "cleared": int}.
        """
        raise NotImplementedError

    def renew_cross_realm(self, principal, realm):
        """Renew a cross-realm TGT only for its own realm.

        Wrong realm raises KerberosError("KRB_AP_ERR_WRONG_REALM").
        """
        raise NotImplementedError

    def check_cache_access(self, owner, requester):
        """Only the owner may read the ticket cache.

        Other requesters raise KerberosError("KRB_CACHE_PERMISSION").
        """
        raise NotImplementedError

    def accept_preauth(self, etype):
        """Require a non-empty pre-authentication type.

        An empty type raises KerberosError("KRB_AP_ERR_PREAUTH_REQUIRED",
        stage="preauth").
        """
        raise NotImplementedError

    def delegate(self, ticket, target_service, allowed_services):
        """Delegate only to explicitly allowed services.

        Other targets raise KerberosError("KRB_DELEGATION_DENIED").
        """
        raise NotImplementedError

    def dump_state(self):
        """Serialize TGTs, replay cache and service tickets."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore state after a restart."""
        raise NotImplementedError
