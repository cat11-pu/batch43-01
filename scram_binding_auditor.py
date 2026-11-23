"""SCRAM channel-binding auditor.

The auditor models the security-sensitive server-side parts of a SCRAM
authentication exchange.  It deliberately keeps authentication state per
connection so a successful exchange can be used to authorize subsequent
commands instead of accidentally continuing with anonymous permissions.
"""

from __future__ import annotations

import hmac
import threading
import unicodedata
from dataclasses import dataclass, field


class ScramError(Exception):
    """Authentication failure raised by the auditor."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


@dataclass
class _Session:
    connection_id: str
    username: str
    original_username: str
    client_nonce: str
    server_nonce: str
    combined_nonce: str
    mechanism: str
    channel_bound: bool
    context: dict = field(default_factory=dict)
    finished: bool = False
    authenticated: bool = False


class ScramBindingAuditor:
    """Validates SCRAM-PLUS channel binding and final signatures."""

    _PLUS_MECHANISMS = frozenset({"SCRAM-SHA-256-PLUS"})
    _PLAIN_MECHANISMS = frozenset({"SCRAM-SHA-256"})
    _SUPPORTED_MECHANISMS = _PLUS_MECHANISMS | _PLAIN_MECHANISMS
    _ABSOLUTE_MIN_ITERATIONS = 4096

    def __init__(self, min_iterations=4096, require_channel_binding=True):
        self.min_iterations = min_iterations
        self.require_channel_binding = require_channel_binding

        self._lock = threading.RLock()
        self._sessions = {}
        self._active_client_nonces = {}
        self._active_server_nonces = {}
        self._seen_final_nonces = set()
        self._authenticated_connections = set()
        self._counters = {
            "auth_attempts": 0,
            "plain_auth_attempts": 0,
            "plus_auth_attempts": 0,
            "downgrade_attempts": 0,
            "channel_binding_failures": 0,
            "nonce_failures": 0,
            "nonce_reuse_failures": 0,
            "iteration_failures": 0,
            "salt_failures": 0,
            "username_failures": 0,
            "authzid_failures": 0,
            "replay_attempts": 0,
            "signature_failures": 0,
            "key_version_failures": 0,
            "connection_in_use_failures": 0,
            "successful_authentications": 0,
            "failed_authentications": 0,
        }

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
        with self._lock:
            self._counters["auth_attempts"] += 1
            # A connection that starts a new exchange must re-prove its
            # identity before it regains authenticated privileges.
            self._authenticated_connections.discard(connection_id)

            normalized_username = self._normalize_identity(
                username, "SCRAM_USERNAME_INVALID"
            )
            normalized_authzid = self._normalize_authzid(authzid)
            normalized_mechanism = self._normalize_mechanism(mechanism)

            if normalized_mechanism in self._PLUS_MECHANISMS:
                self._counters["plus_auth_attempts"] += 1
                channel_bound, endpoint_binding = self._verify_channel_binding(
                    cert_chain, channel_binding
                )
            else:
                self._counters["plain_auth_attempts"] += 1
                if self.require_channel_binding:
                    self._counters["downgrade_attempts"] += 1
                    self._fail_begin("SCRAM_CHANNEL_BINDING_REQUIRED")
                channel_bound, endpoint_binding = False, None

            if normalized_authzid is not None and normalized_authzid != normalized_username:
                self._counters["authzid_failures"] += 1
                self._fail_begin("SCRAM_AUTHZID_DENIED")

            self._validate_iterations(iterations)
            salt_bytes = self._validate_salt(salt)
            self._validate_key_versions(stored_key_version, derived_key_version)

            client_nonce_text = self._normalize_nonce(client_nonce)
            server_nonce_text = self._normalize_nonce(server_nonce)
            combined_nonce = client_nonce_text + server_nonce_text

            nonce_key = (normalized_username, client_nonce_text)
            if nonce_key in self._active_client_nonces:
                self._counters["nonce_reuse_failures"] += 1
                self._fail_begin("SCRAM_NONCE_REUSE")
            server_nonce_key = (normalized_username, server_nonce_text)
            if server_nonce_key in self._active_server_nonces:
                self._counters["nonce_reuse_failures"] += 1
                self._fail_begin("SCRAM_NONCE_REUSE")

            existing = self._sessions.get(connection_id)
            if existing is not None and not existing.finished:
                self._counters["connection_in_use_failures"] += 1
                self._fail_begin("SCRAM_CONNECTION_IN_USE")

            if existing is not None:
                self._authenticated_connections.discard(connection_id)

            context = {
                "connection_id": connection_id,
                "username": normalized_username,
                "normalized_username": normalized_username,
                "original_username": self._original_identity(username),
                "client_nonce": client_nonce_text,
                "server_nonce": server_nonce_text,
                "combined_nonce": combined_nonce,
                "mechanism": normalized_mechanism,
                "channel_bound": channel_bound,
                "channel_binding_used": channel_bound,
                "endpoint_binding": endpoint_binding,
                "iterations": iterations,
                "salt": salt_bytes,
                "stored_key_version": stored_key_version,
                "derived_key_version": derived_key_version,
                "authenticated": False,
            }
            session = _Session(
                connection_id=connection_id,
                username=normalized_username,
                original_username=context["original_username"],
                client_nonce=client_nonce_text,
                server_nonce=server_nonce_text,
                combined_nonce=combined_nonce,
                mechanism=normalized_mechanism,
                channel_bound=channel_bound,
                context=context,
            )
            self._sessions[connection_id] = session
            self._active_client_nonces[nonce_key] = connection_id
            self._active_server_nonces[server_nonce_key] = connection_id
            return context

    def finish(
        self,
        connection_id,
        client_final_nonce,
        server_signature_valid=True,
        channel_binding_used=True,
    ):
        with self._lock:
            session = self._sessions.get(connection_id)
            if session is None:
                self._fail_begin("SCRAM_SESSION_NOT_FOUND")

            if session.finished:
                self._counters["replay_attempts"] += 1
                self._fail_begin("SCRAM_REPLAY")

            final_nonce = self._normalize_nonce(client_final_nonce)
            if final_nonce in self._seen_final_nonces:
                self._counters["replay_attempts"] += 1
                self._retire(session)
                self._fail_begin("SCRAM_REPLAY")

            if (
                not final_nonce.startswith(session.client_nonce)
                or not hmac.compare_digest(
                    final_nonce.encode("utf-8"),
                    session.combined_nonce.encode("utf-8"),
                )
            ):
                self._counters["nonce_failures"] += 1
                self._fail_begin("SCRAM_NONCE_INVALID")

            if session.channel_bound and not channel_binding_used:
                self._counters["channel_binding_failures"] += 1
                session.finished = True
                session.authenticated = False
                session.context["authenticated"] = False
                session.context["channel_binding_used"] = False
                self._retire(session)
                self._fail_begin("SCRAM_CHANNEL_BINDING_REQUIRED")

            if self.require_channel_binding and not session.channel_bound:
                self._counters["channel_binding_failures"] += 1
                session.finished = True
                session.authenticated = False
                session.context["authenticated"] = False
                self._retire(session)
                self._fail_begin("SCRAM_CHANNEL_BINDING_REQUIRED")

            # A final message is single-use even when its proof is invalid.
            self._seen_final_nonces.add(final_nonce)
            self._retire(session)
            session.finished = True

            if not server_signature_valid:
                self._counters["signature_failures"] += 1
                self._counters["failed_authentications"] += 1
                session.authenticated = False
                session.context["authenticated"] = False
                return {
                    "authenticated": False,
                    "code": "SCRAM_SIGNATURE_INVALID",
                    "connection_id": connection_id,
                    "username": session.username,
                    "mechanism": session.mechanism,
                }

            self._counters["successful_authentications"] += 1
            session.authenticated = True
            session.context["authenticated"] = True
            session.context["channel_binding_used"] = session.channel_bound
            self._authenticated_connections.add(connection_id)
            return {
                "authenticated": True,
                "code": None,
                "connection_id": connection_id,
                "username": session.username,
                "mechanism": session.mechanism,
                "channel_binding_used": session.channel_bound,
            }

    def is_authenticated(self, connection_id):
        with self._lock:
            return connection_id in self._authenticated_connections

    def counters(self):
        with self._lock:
            result = dict(self._counters)
            result["authenticated_connections"] = len(
                self._authenticated_connections
            )
            result["active_sessions"] = sum(
                1 for session in self._sessions.values() if not session.finished
            )
            return result

    # ------------------------------------------------------------------
    # Validation helpers

    def _verify_channel_binding(self, cert_chain, channel_binding):
        if channel_binding is None:
            self._counters["channel_binding_failures"] += 1
            self._fail_begin("SCRAM_CHANNEL_BINDING_REQUIRED")

        leaf = self._leaf_certificate(cert_chain)
        if leaf is None:
            self._counters["channel_binding_failures"] += 1
            self._fail_begin("SCRAM_CHANNEL_BINDING_REQUIRED")

        actual = self._as_bytes(channel_binding)
        if actual is None or not hmac.compare_digest(actual, leaf):
            self._counters["channel_binding_failures"] += 1
            self._fail_begin("SCRAM_CHANNEL_BINDING_MISMATCH")

        return True, leaf

    @classmethod
    def _leaf_certificate(cls, cert_chain):
        if cert_chain is None:
            return None

        if isinstance(cert_chain, (bytes, bytearray, memoryview, str)):
            chain = [cert_chain]
        else:
            try:
                chain = list(cert_chain)
            except TypeError:
                chain = [cert_chain]

        if not chain:
            return None

        # Certificate chains are ordered from the root/intermediates to the
        # leaf in this interface; the endpoint binding must use the leaf.
        leaf = chain[-1]
        if isinstance(leaf, dict):
            for key in ("der", "cert", "certificate", "value", "data"):
                if key in leaf:
                    leaf = leaf[key]
                    break
        return cls._as_bytes(leaf)

    @staticmethod
    def _as_bytes(value):
        if value is None:
            return None
        if isinstance(value, bytes):
            return value
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        return None

    @staticmethod
    def _original_identity(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            try:
                return bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                return None
        return value

    def _normalize_identity(self, value, error_code):
        if isinstance(value, (bytes, bytearray, memoryview)):
            try:
                value = bytes(value).decode("utf-8")
            except UnicodeDecodeError:
                self._counters["username_failures"] += 1
                self._fail_begin(error_code)
        elif not isinstance(value, str):
            self._counters["username_failures"] += 1
            self._fail_begin(error_code)

        if not value:
            self._counters["username_failures"] += 1
            self._fail_begin(error_code)

        return unicodedata.normalize("NFKC", value)

    def _normalize_authzid(self, authzid):
        if authzid is None:
            return None
        if isinstance(authzid, (bytes, bytearray, memoryview)):
            try:
                authzid = bytes(authzid).decode("utf-8")
            except UnicodeDecodeError:
                self._counters["authzid_failures"] += 1
                self._fail_begin("SCRAM_AUTHZID_DENIED")
        elif not isinstance(authzid, str):
            self._counters["authzid_failures"] += 1
            self._fail_begin("SCRAM_AUTHZID_DENIED")

        if authzid == "":
            return None
        return unicodedata.normalize("NFKC", authzid)

    def _normalize_mechanism(self, mechanism):
        if not isinstance(mechanism, str):
            self._fail_begin("SCRAM_MECHANISM_UNSUPPORTED")
        normalized = mechanism.upper()
        if normalized not in self._SUPPORTED_MECHANISMS:
            self._fail_begin("SCRAM_MECHANISM_UNSUPPORTED")
        return normalized

    def _normalize_nonce(self, nonce):
        if isinstance(nonce, (bytes, bytearray, memoryview)):
            try:
                nonce = bytes(nonce).decode("utf-8")
            except UnicodeDecodeError:
                self._counters["nonce_failures"] += 1
                self._fail_begin("SCRAM_NONCE_INVALID")
        elif not isinstance(nonce, str):
            self._counters["nonce_failures"] += 1
            self._fail_begin("SCRAM_NONCE_INVALID")

        if not nonce:
            self._counters["nonce_failures"] += 1
            self._fail_begin("SCRAM_NONCE_INVALID")
        return nonce

    def _validate_iterations(self, iterations):
        minimum = self._ABSOLUTE_MIN_ITERATIONS
        if isinstance(self.min_iterations, int) and not isinstance(
            self.min_iterations, bool
        ):
            minimum = max(minimum, self.min_iterations)

        if isinstance(iterations, bool) or not isinstance(iterations, int):
            if isinstance(iterations, float) and iterations.is_integer():
                iterations = int(iterations)
            else:
                self._counters["iteration_failures"] += 1
                self._fail_begin("SCRAM_ITERATIONS_TOO_LOW")

        if iterations < minimum:
            self._counters["iteration_failures"] += 1
            self._fail_begin("SCRAM_ITERATIONS_TOO_LOW")

    def _validate_salt(self, salt):
        if salt is None:
            self._counters["salt_failures"] += 1
            self._fail_begin("SCRAM_SALT_INVALID")
        if isinstance(salt, str):
            salt_bytes = salt.encode("utf-8")
        elif isinstance(salt, (bytes, bytearray, memoryview)):
            salt_bytes = bytes(salt)
        else:
            self._counters["salt_failures"] += 1
            self._fail_begin("SCRAM_SALT_INVALID")

        if not salt_bytes:
            self._counters["salt_failures"] += 1
            self._fail_begin("SCRAM_SALT_INVALID")
        return salt_bytes

    def _validate_key_versions(self, stored_key_version, derived_key_version):
        valid = all(
            isinstance(version, int)
            and not isinstance(version, bool)
            and version >= 1
            for version in (stored_key_version, derived_key_version)
        )
        if not valid or stored_key_version != derived_key_version:
            self._counters["key_version_failures"] += 1
            self._fail_begin("SCRAM_KEY_VERSION")

    def _retire(self, session):
        self._active_client_nonces.pop(
            (session.username, session.client_nonce), None
        )
        self._active_server_nonces.pop(
            (session.username, session.server_nonce), None
        )

    def _fail_begin(self, code):
        self._counters["failed_authentications"] += 1
        raise ScramError(code)
