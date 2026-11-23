"""Local Kerberos ticket cache vault.

Hardens the local TGT/service-ticket cache while the KDC itself stays under
central administration.  Every rejection carries a Kerberos-style code and
the failing ``stage`` so the security team can trace an authentication
failure to the exact check that rejected it.
"""

import copy
import threading


class KerberosError(Exception):
    """Error carrying a Kerberos code and failing stage."""

    def __init__(self, code, detail="", stage=""):
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.stage = stage


class KerberosTicketVault:
    """Local TGT cache, replay cache and service-ticket bookkeeping."""

    #: Lifetime granted by one successful renewal, in seconds.
    RENEW_LIFETIME = 3600
    #: Ticket cache files are owner read/write only.
    CACHE_MODE = 0o600

    def __init__(self, clock_skew=300, max_renewable=86400):
        self.clock_skew = clock_skew
        self.max_renewable = max_renewable
        self._tickets = {}
        self._replay_cache = {}
        self._service_tickets = {}
        self._active_principal = None
        self._lock = threading.RLock()

    def store_tgt(self, principal, ticket, key_version=1):
        """Store a TGT; ticket has ticket_id, expires_at, renew_till, realm.

        Returns {"stored": True, "key_version": int}.
        """
        with self._lock:
            stored = copy.deepcopy(ticket)
            stored["key_version"] = key_version
            # A ticket must not be renewable past expires_at + max_renewable,
            # even when the KDC-issued renew_till claims a longer window.
            renewable_cap = stored["expires_at"] + self.max_renewable
            stored["renew_till_effective"] = min(
                stored.get("renew_till", renewable_cap), renewable_cap
            )
            stored["renewed_at"] = None
            stored["last_renewal"] = None
            self._tickets[principal] = stored
            self._replay_cache.setdefault(principal, set())
            self._service_tickets.setdefault(principal, {})
            if self._active_principal is None:
                self._active_principal = principal
            return {"stored": True, "key_version": key_version}

    def renew(self, principal, now, client_time=None):
        """Renew a TGT.

        Expired TGT raises KerberosError("KRB_AP_ERR_TKT_EXPIRED",
        stage="renew"); too old for renewal raises
        "KRB_AP_ERR_RENEW_EXPIRED"; a client_time outside ``clock_skew``
        raises "KRB_AP_ERR_SKEW".  Returns {"renewed": True, "ticket_id": str,
        "expires_at": int}.
        """
        with self._lock:
            record = self._tickets.get(principal)
            if record is None:
                raise KerberosError(
                    "KRB_NO_TICKET",
                    "no cached TGT for principal %r" % principal,
                    stage="renew",
                )

            # Clock skew is checked first: an out-of-sync client must never
            # have an expired ticket silently treated as valid.
            if client_time is not None and abs(client_time - now) > self.clock_skew:
                raise KerberosError(
                    "KRB_AP_ERR_SKEW",
                    "clock skew %ds exceeds limit %ds"
                    % (abs(client_time - now), self.clock_skew),
                    stage="renew",
                )

            if now >= record["expires_at"]:
                raise KerberosError(
                    "KRB_AP_ERR_TKT_EXPIRED",
                    "TGT %r expired at %s (now %s)"
                    % (record["ticket_id"], record["expires_at"], now),
                    stage="renew",
                )

            renew_till = record.get(
                "renew_till_effective", record.get("renew_till", now)
            )
            if now > renew_till:
                raise KerberosError(
                    "KRB_AP_ERR_RENEW_EXPIRED",
                    "renewal window closed at %s (now %s)" % (renew_till, now),
                    stage="renew",
                )

            # Concurrent renewals in the same renewal window must converge on
            # a single renewed TGT instead of producing two ticket roots.
            if record.get("renewed_at") == now and record.get("last_renewal"):
                return dict(record["last_renewal"])

            # Never extend the ticket past its effective renewable boundary.
            new_expires_at = min(
                record["expires_at"] + self.RENEW_LIFETIME, renew_till
            )
            result = {
                "renewed": True,
                "ticket_id": record["ticket_id"],
                "expires_at": new_expires_at,
            }
            record["expires_at"] = new_expires_at
            record["renewed_at"] = now
            record["last_renewal"] = dict(result)
            return dict(result)

    def check_keytab(self, principal, key_version):
        """Verify keytab version; mismatch raises "KRB_AP_ERR_BADKEYVER"."""
        with self._lock:
            record = self._tickets.get(principal)
            expected = record["key_version"] if record is not None else None
            if expected != key_version:
                raise KerberosError(
                    "KRB_AP_ERR_BADKEYVER",
                    "keytab version %s does not match cached version %s"
                    % (key_version, expected),
                    stage="keytab",
                )
            return {"matched": True, "key_version": key_version}

    def authenticate(self, principal, authenticator_id):
        """Authenticate an authenticator exactly once.

        A repeated authenticator raises KerberosError("KRB_AP_ERR_REPLAY").
        """
        with self._lock:
            seen = self._replay_cache.setdefault(principal, set())
            if authenticator_id in seen:
                raise KerberosError(
                    "KRB_AP_ERR_REPLAY",
                    "authenticator %r was already used" % authenticator_id,
                    stage="authenticate",
                )
            seen.add(authenticator_id)
            return {
                "authenticated": True,
                "authenticator_id": authenticator_id,
            }

    def set_service_ticket(self, principal, service, ticket_id):
        """Cache a service ticket for a principal."""
        with self._lock:
            self._service_tickets.setdefault(principal, {})[service] = ticket_id
            return {"cached": True, "principal": principal, "service": service}

    def get_service_ticket(self, principal, service):
        """Return the cached service ticket or raise "KRB_NO_TICKET"."""
        with self._lock:
            tickets = self._service_tickets.get(principal, {})
            if service not in tickets:
                raise KerberosError(
                    "KRB_NO_TICKET",
                    "no service ticket for %r on %r" % (principal, service),
                    stage="service_ticket",
                )
            return tickets[service]

    def switch_principal(self, principal):
        """Switch the active principal and clear the previous cache.

        Returns {"current": str, "cleared": int}.
        """
        with self._lock:
            cleared = 0
            if self._active_principal is not None and self._active_principal != principal:
                previous = self._service_tickets.get(self._active_principal, {})
                cleared = len(previous)
                previous.clear()
            self._active_principal = principal
            self._service_tickets.setdefault(principal, {})
            return {"current": principal, "cleared": cleared}

    def renew_cross_realm(self, principal, realm):
        """Renew a cross-realm TGT only for its own realm.

        Wrong realm raises KerberosError("KRB_AP_ERR_WRONG_REALM").
        """
        with self._lock:
            record = self._tickets.get(principal)
            ticket_realm = record["realm"] if record is not None else None
            if ticket_realm != realm:
                raise KerberosError(
                    "KRB_AP_ERR_WRONG_REALM",
                    "realm %r is not the issuing realm %r"
                    % (realm, ticket_realm),
                    stage="cross_realm",
                )
            return {
                "renewed": True,
                "realm": realm,
                "ticket_id": record["ticket_id"],
            }

    def check_cache_access(self, owner, requester):
        """Only the owner may read the ticket cache.

        Other requesters raise KerberosError("KRB_CACHE_PERMISSION").
        """
        if owner != requester:
            raise KerberosError(
                "KRB_CACHE_PERMISSION",
                "cache mode is %o; %r may not read cache owned by %r"
                % (self.CACHE_MODE, requester, owner),
                stage="cache_access",
            )
        return {"allowed": True, "mode": oct(self.CACHE_MODE)}

    def accept_preauth(self, etype):
        """Require a non-empty pre-authentication type.

        An empty type raises KerberosError("KRB_AP_ERR_PREAUTH_REQUIRED",
        stage="preauth").
        """
        if not etype or not str(etype).strip():
            raise KerberosError(
                "KRB_AP_ERR_PREAUTH_REQUIRED",
                "pre-authentication with an empty etype is not permitted",
                stage="preauth",
            )
        return {"accepted": True, "etype": etype}

    def delegate(self, ticket, target_service, allowed_services):
        """Delegate only to explicitly allowed services.

        Other targets raise KerberosError("KRB_DELEGATION_DENIED").
        """
        if target_service not in allowed_services:
            raise KerberosError(
                "KRB_DELEGATION_DENIED",
                "service %r is not in the allowed delegation list" % target_service,
                stage="delegation",
            )
        return {
            "delegated": True,
            "ticket": ticket,
            "target": target_service,
        }

    def dump_state(self):
        """Serialize TGTs, replay cache and service tickets."""
        with self._lock:
            return {
                "tickets": copy.deepcopy(self._tickets),
                "replay_cache": {
                    principal: sorted(authenticators)
                    for principal, authenticators in self._replay_cache.items()
                },
                "service_tickets": copy.deepcopy(self._service_tickets),
                "active_principal": self._active_principal,
            }

    def load_state(self, state):
        """Restore state after a restart."""
        with self._lock:
            self._tickets = copy.deepcopy(state.get("tickets", {}))
            self._replay_cache = {
                principal: set(authenticators)
                for principal, authenticators in state.get("replay_cache", {}).items()
            }
            self._service_tickets = copy.deepcopy(state.get("service_tickets", {}))
            self._active_principal = state.get("active_principal")
            return {"loaded": True}
