"""LDAP bind pool with fail-closed authorization identity handling."""

from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


class LdapError(Exception):
    """Error carrying a stable LDAP error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _split_unescaped(value, separator):
    """Split ``value`` on unescaped separators outside quoted strings."""
    parts = []
    start = 0
    escaped = False
    quoted = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if char == separator and not quoted:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _split_first_unescaped(value, separator):
    """Split at the first unescaped separator, returning both sides."""
    escaped = False
    quoted = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if char == separator and not quoted:
            return value[:index], value[index + 1:]
    return None, value


def _normalize_dn(dn):
    """Return a canonical DN for case-insensitive and space-insensitive use."""
    if dn is None:
        return ""
    text = str(dn).strip()
    if not text:
        return ""

    normalized_rdns = []
    for rdn in _split_unescaped(text, ","):
        normalized_rdn = []
        for attribute_value in _split_unescaped(rdn, "+"):
            attribute, value = _split_first_unescaped(attribute_value, "=")
            if attribute is None:
                normalized_rdn.append(attribute_value.strip().lower())
                continue
            attribute = attribute.strip().lower()
            value = value.strip().lower()
            normalized_rdn.append(f"{attribute}={value}")
        normalized_rdns.append("+".join(sorted(normalized_rdn)))
    return ",".join(normalized_rdns)


def _is_within_dn(dn, base):
    """Return whether ``dn`` is ``base`` or a descendant of ``base``."""
    if not dn or not base:
        return False
    return dn == base or dn.endswith("," + base)


def _has_credentials(password):
    return password is not None and str(password).strip() != ""


@dataclass
class _PageState:
    query_id: str
    base: str
    page: int


@dataclass
class _Connection:
    handle: int
    lock: threading.RLock = field(default_factory=threading.RLock)
    checked_out: bool = True
    connected: bool = False
    tls: bool = False
    identity: str | None = None
    roles: set[str] = field(default_factory=set)
    page_cookies: dict[str, _PageState] = field(default_factory=dict)


class LdapBindPool:
    """Pooled LDAP connections with per-bind authorization identity."""

    _PUBLIC_ROOT_DSE = {
        "vendorName": "GSB Directory",
        "vendorVersion": "1.0",
        "supportedLDAPVersion": ["3"],
        "namingContexts": ["dc=corp,dc=local"],
    }

    def __init__(self, pool_size=4,
                 referral_whitelist=("dc=corp,dc=local",),
                 tls_required=True,
                 sasl_mechanisms=("EXTERNAL", "GSSAPI")):
        self.pool_size = int(pool_size)
        if self.pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        if isinstance(referral_whitelist, str):
            referral_whitelist = (referral_whitelist,)
        self.referral_whitelist = tuple(
            normalized for normalized in (
                _normalize_dn(base) for base in referral_whitelist
            ) if normalized
        )
        self.tls_required = bool(tls_required)
        if isinstance(sasl_mechanisms, str):
            sasl_mechanisms = (sasl_mechanisms,)
        self.sasl_mechanisms = tuple(
            str(mechanism).strip().upper()
            for mechanism in sasl_mechanisms
            if str(mechanism).strip()
        )

        self._lock = threading.RLock()
        self._audit_lock = threading.Lock()
        self._connections: dict[int, _Connection] = {}
        self._available: list[int] = []
        self._next_handle = 1
        self._locked_accounts: set[str] = set()
        self._audit: list[dict] = []

    def acquire(self):
        """Check out a pooled connection; returns {"handle": int}."""
        with self._lock:
            if self._available:
                handle = self._available.pop()
                connection = self._connections[handle]
                connection.checked_out = True
                return {"handle": handle}

            if len(self._connections) >= self.pool_size:
                raise LdapError(
                    "LDAP_POOL_EXHAUSTED",
                    "all pooled connections are checked out",
                )

            handle = self._next_handle
            self._next_handle += 1
            self._connections[handle] = _Connection(handle=handle)
            return {"handle": handle}

    def release(self, handle):
        """Return a connection to the pool and clear its identity."""
        connection = self._lookup(handle)
        with connection.lock:
            connection.identity = None
            connection.roles.clear()
            connection.page_cookies.clear()
            connection.checked_out = False

        with self._lock:
            if handle not in self._available:
                self._available.append(handle)
        return {"released": True}

    def connect(self, handle, starttls=True):
        """Set up the transport; returns {"connected": True, "tls": bool}."""
        with self._active_connection(handle) as connection:
            connection.connected = True
            connection.tls = bool(starttls)
            connection.identity = None
            connection.roles.clear()
            connection.page_cookies.clear()
            return {"connected": True, "tls": connection.tls}

    def bind(self, handle, dn, password):
        """Bind a user; empty credentials raise "LDAP_ANONYMOUS_DENIED".

        Returns {"bound_dn": str} with the normalized DN.  A locked account
        raises "LDAP_ACCOUNT_LOCKED" even on an already bound connection.
        """
        with self._active_connection(handle) as connection:
            connection.identity = None
            connection.roles.clear()
            connection.page_cookies.clear()
            normalized_dn = self._validate_bind(connection, dn, password)
            connection.identity = normalized_dn
            return {"bound_dn": normalized_dn}

    def bind_service(self, handle, service_dn, password):
        """Bind the pool's service account (administrative identity)."""
        with self._active_connection(handle) as connection:
            connection.identity = None
            connection.roles.clear()
            connection.page_cookies.clear()
            normalized_dn = self._validate_bind(
                connection, service_dn, password,
            )
            connection.identity = normalized_dn
            connection.roles = {"admin"}
            return {"bound_dn": normalized_dn}

    def rebind(self, handle, dn, password):
        """Atomically replace the authorization identity.

        The previous identity and roles must not survive.  Emits an audit entry
        with the previous and new identity.  Returns {"bound_dn": str}.
        """
        with self._active_connection(handle) as connection:
            previous = connection.identity
            attempted = _normalize_dn(dn)

            connection.identity = None
            connection.roles.clear()
            connection.page_cookies.clear()

            try:
                normalized_dn = self._validate_bind(connection, dn, password)
            except LdapError as error:
                self._record_audit(
                    handle, previous, attempted, success=False,
                    error_code=error.code,
                )
                raise

            connection.identity = normalized_dn
            self._record_audit(
                handle, previous, normalized_dn, success=True,
            )
            return {"bound_dn": normalized_dn}

    def current_identity(self, handle):
        """Return the normalized bound DN for a connection, or None."""
        connection = self._lookup(handle)
        with connection.lock:
            return connection.identity

    def has_role(self, handle, role):
        """Return whether the current identity carries a role."""
        connection = self._lookup(handle)
        with connection.lock:
            if connection.identity is None:
                return False
            return str(role).strip().lower() in connection.roles

    def lock_account(self, dn):
        """Lock an account; returns {"locked": True}."""
        normalized_dn = _normalize_dn(dn)
        if not normalized_dn:
            raise LdapError(
                "LDAP_INVALID_DN", "an account DN is required",
            )

        with self._lock:
            self._locked_accounts.add(normalized_dn)
            connections = list(self._connections.values())

        for connection in connections:
            with connection.lock:
                if connection.identity == normalized_dn:
                    connection.identity = None
                    connection.roles.clear()
                    connection.page_cookies.clear()
        return {"locked": True}

    def is_locked(self, dn):
        """Return whether an account is locked."""
        normalized_dn = _normalize_dn(dn)
        with self._lock:
            return normalized_dn in self._locked_accounts

    def follow_referral(self, handle, target_dn):
        """Follow a referral only inside the whitelist.

        Outside targets raise LdapError("LDAP_REFERRAL_BLOCKED").
        """
        normalized_target = _normalize_dn(target_dn)
        with self._active_connection(handle):
            if not any(
                _is_within_dn(normalized_target, whitelisted)
                for whitelisted in self.referral_whitelist
            ):
                raise LdapError(
                    "LDAP_REFERRAL_BLOCKED",
                    f"referral target is outside the whitelist: "
                    f"{normalized_target or '<empty>'}",
                )
            return {"followed": True, "dn": normalized_target}

    def search(self, handle, base, page_cookie=None, query_id="default"):
        """Run a paged search bound to one query.

        Reusing a cookie under another query id raises
        LdapError("LDAP_COOKIE_REPLAY").  Returns {"cookie": str|None,
        "results": list}.
        """
        query_key = "default" if query_id is None else str(query_id)
        normalized_base = _normalize_dn(base)

        with self._active_connection(handle) as connection:
            page = 1
            if page_cookie is not None:
                cookie = str(page_cookie)
                state = connection.page_cookies.pop(cookie, None)
                if (
                    state is None
                    or state.query_id != query_key
                    or state.base != normalized_base
                ):
                    raise LdapError(
                        "LDAP_COOKIE_REPLAY",
                        "pagination cookie does not belong to this query",
                    )
                page = state.page + 1

            next_cookie = secrets.token_urlsafe(24)
            connection.page_cookies[next_cookie] = _PageState(
                query_id=query_key,
                base=normalized_base,
                page=page,
            )
            return {
                "cookie": next_cookie,
                "results": [],
            }

    def sasl_bind(self, handle, mechanism):
        """SASL bind that refuses downgrade to SIMPLE.

        Unsupported mechanisms raise LdapError("LDAP_SASL_DOWNGRADE").
        """
        selected = "" if mechanism is None else str(mechanism).strip().upper()
        with self._active_connection(handle) as connection:
            connection.identity = None
            connection.roles.clear()
            connection.page_cookies.clear()
            self._require_tls(connection)
            if selected == "SIMPLE" or selected not in self.sasl_mechanisms:
                raise LdapError(
                    "LDAP_SASL_DOWNGRADE",
                    f"SASL mechanism is not permitted: {selected or '<empty>'}",
                )
            return {"bound": True, "mechanism": selected}

    def root_dse(self):
        """Return the public root DSE attributes only."""
        dse = dict(self._PUBLIC_ROOT_DSE)
        dse["supportedLDAPVersion"] = list(
            self._PUBLIC_ROOT_DSE["supportedLDAPVersion"],
        )
        dse["namingContexts"] = list(
            self._PUBLIC_ROOT_DSE["namingContexts"],
        )
        if self.sasl_mechanisms:
            dse["supportedSASLMechanisms"] = list(self.sasl_mechanisms)
        return dse

    def audit_log(self):
        """Return bind audit entries with previous and new identities."""
        with self._audit_lock:
            return [dict(entry) for entry in self._audit]

    def _lookup(self, handle):
        with self._lock:
            try:
                return self._connections[handle]
            except (KeyError, TypeError):
                raise LdapError(
                    "LDAP_INVALID_HANDLE", f"unknown connection handle: {handle}",
                ) from None

    @contextmanager
    def _active_connection(self, handle):
        connection = self._lookup(handle)
        with connection.lock:
            if not connection.checked_out:
                raise LdapError(
                    "LDAP_INVALID_HANDLE",
                    f"connection {handle} is not checked out",
                )
            yield connection

    def _require_tls(self, connection):
        if self.tls_required and not connection.tls:
            raise LdapError(
                "LDAP_TLS_REQUIRED",
                "StartTLS is required before binding",
            )

    def _validate_bind(self, connection, dn, password):
        normalized_dn = _normalize_dn(dn)
        if not normalized_dn or not _has_credentials(password):
            raise LdapError(
                "LDAP_ANONYMOUS_DENIED",
                "anonymous and empty-credential binds are not permitted",
            )
        with self._lock:
            locked = normalized_dn in self._locked_accounts
        if locked:
            raise LdapError(
                "LDAP_ACCOUNT_LOCKED",
                f"account is locked: {normalized_dn}",
            )
        self._require_tls(connection)
        return normalized_dn

    def _record_audit(self, handle, previous, new, success, error_code=None):
        entry = {
            "handle": handle,
            "previous": previous,
            "new": new,
            "success": bool(success),
            "at": time.time(),
        }
        if error_code is not None:
            entry["error"] = error_code
        with self._audit_lock:
            self._audit.append(entry)
