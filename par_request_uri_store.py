"""Pushed authorization request URI store."""

from __future__ import annotations

import copy
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported deployment is POSIX.
    fcntl = None


_REQUEST_URI_PREFIX = "urn:ietf:params:oauth:request_uri:"
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class ParError(Exception):
    """Authorization failure raised by the store."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


def _empty_state():
    return {
        "version": 1,
        "request_uris": {},
        "codes": {},
        "audit": [],
    }


def _json_safe(value):
    """Convert request data to a stable, JSON-serializable audit value."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return repr(value)


def _path_lock(path):
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[path] = lock
    return lock


class ParRequestUriStore:
    """Stores pushed authorization requests with client binding and single use."""

    def __init__(self, state_path=None, request_uri_ttl=90.0, require_pkce_s256=True):
        self.state_path = os.path.abspath(os.fspath(state_path)) if state_path else None
        self.request_uri_ttl = self._positive_number(request_uri_ttl, "request_uri_ttl")
        self.require_pkce_s256 = bool(require_pkce_s256)
        self._lock = threading.RLock()
        self._state = _empty_state()
        if self.state_path is not None:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with self._locked():
                self._state = self._read_state()

    @staticmethod
    def _positive_number(value, name):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a positive number") from exc
        if number <= 0:
            raise ValueError(f"{name} must be a positive number")
        return number

    @contextmanager
    def _locked(self):
        if self.state_path is None:
            with self._lock:
                yield
            return

        path_lock = _path_lock(self.state_path)
        with path_lock:
            with self._lock:
                lock_path = self.state_path + ".lock"
                with open(lock_path, "a+", encoding="utf-8") as lock_file:
                    if fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        if fcntl is not None:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_state(self):
        if self.state_path is None:
            return self._state
        try:
            with open(self.state_path, "r", encoding="utf-8") as state_file:
                state = json.load(state_file)
        except FileNotFoundError:
            return _empty_state()

        if not isinstance(state, dict):
            raise ValueError("PAR state file is invalid")
        state.setdefault("version", 1)
        state.setdefault("request_uris", {})
        state.setdefault("codes", {})
        state.setdefault("audit", [])
        return state

    def _write_state(self, state):
        if self.state_path is None:
            self._state = state
            return

        directory = os.path.dirname(self.state_path)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".par-state-", dir=directory, text=True
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as state_file:
                json.dump(state, state_file, sort_keys=True, separators=(",", ":"))
                state_file.flush()
                os.fsync(state_file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.state_path)
        except BaseException:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise

    def _execute(self, operation):
        """Run one read-modify-write transaction under a cross-process lock."""

        with self._locked():
            state = self._read_state()
            try:
                return operation(state)
            finally:
                self._write_state(state)

    @staticmethod
    def _audit(state, event, result, error_code=None, **details):
        entry = {
            "at": time.time(),
            "event": event,
            "result": result,
        }
        if error_code is not None:
            entry["error"] = error_code
        entry.update({key: _json_safe(value) for key, value in details.items()})
        state["audit"].append(entry)

    @staticmethod
    def _normalise_request(request):
        """Return request parameters and detect repeated scalar parameters."""

        duplicate = False
        if isinstance(request, Mapping):
            getlist = getattr(request, "getlist", None)
            if callable(getlist):
                for key in set(request.keys()):
                    try:
                        values = getlist(key)
                    except (KeyError, TypeError):
                        continue
                    if len(values) > 1:
                        duplicate = True
                        break

            params = {}
            for key, value in request.items():
                if key in params:
                    duplicate = True
                if isinstance(value, (list, tuple)):
                    if len(value) > 1:
                        duplicate = True
                    value = value[0] if value else ""
                params[key] = value
            return params, duplicate

        if isinstance(request, (list, tuple)):
            params = {}
            for item in request:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    raise ParError("invalid_request", "malformed request parameters")
                key, value = item
                if key in params:
                    duplicate = True
                else:
                    params[key] = value
            return params, duplicate

        raise ParError("invalid_request", "request must be a parameter mapping")

    @staticmethod
    def _scope_set(value):
        if value is None:
            return set()
        if isinstance(value, str):
            return {item for item in value.split() if item}
        if isinstance(value, (list, tuple, set, frozenset)):
            return {str(item) for item in value if str(item)}
        raise ParError("invalid_request", "scope must be a string or list")

    @staticmethod
    def _request_time(params, nested):
        for source in (nested, params):
            for key in ("pushed_at", "issued_at", "created_at", "now"):
                if key in source:
                    try:
                        return float(source[key])
                    except (TypeError, ValueError) as exc:
                        raise ParError("invalid_request", "invalid request timestamp") from exc
        return 0.0

    def push(self, request, signature_valid=True):
        """Validate and persist a pushed authorization request."""

        def operation(state):
            try:
                params, duplicate = self._normalise_request(request)
                if duplicate:
                    raise ParError("invalid_request", "repeated request parameter")
                if not signature_valid:
                    raise ParError("invalid_request", "invalid request object signature")

                nested = params.get("request")
                if not isinstance(nested, Mapping):
                    nested = {}
                else:
                    _, nested_duplicate = self._normalise_request(nested)
                    if nested_duplicate:
                        raise ParError("invalid_request", "repeated request parameter")

                client_values = [
                    value
                    for value in (
                        params.get("client_id"),
                        params.get("request_client_id"),
                        nested.get("client_id"),
                        nested.get("request_client_id"),
                    )
                    if value is not None
                ]
                if not client_values:
                    raise ParError("invalid_client", "missing client identifier")
                client_id = client_values[0]
                if not isinstance(client_id, str) or not client_id:
                    raise ParError("invalid_client", "missing client identifier")
                if any(value != client_id for value in client_values):
                    raise ParError("invalid_client", "client identifier mismatch")

                scope_values = [
                    source[key]
                    for source in (params, nested)
                    for key in ("scope", "requested_scope")
                    if key in source
                ]
                scopes = [self._scope_set(value) for value in scope_values]
                if scopes and any(scope != scopes[0] for scope in scopes[1:]):
                    raise ParError("invalid_request", "requested scope mismatch")
                outer_scope = scopes[0] if scopes else set()

                redirect_values = [
                    source["redirect_uri"]
                    for source in (params, nested)
                    if "redirect_uri" in source
                ]
                if redirect_values and any(
                    value != redirect_values[0] for value in redirect_values[1:]
                ):
                    raise ParError("invalid_request", "redirect_uri mismatch")
                redirect_uri = redirect_values[0] if redirect_values else None
                if not isinstance(redirect_uri, str) or not redirect_uri:
                    raise ParError("invalid_request", "missing redirect_uri")

                method_values = [
                    source["code_challenge_method"]
                    for source in (params, nested)
                    if "code_challenge_method" in source
                ]
                challenge_method = method_values[0] if method_values else None
                if method_values and any(
                    value != challenge_method for value in method_values[1:]
                ):
                    raise ParError("invalid_request", "code challenge method mismatch")
                if self.require_pkce_s256 and challenge_method != "S256":
                    raise ParError("invalid_request", "S256 PKCE is required")

                ttl = nested.get("ttl", params.get("ttl", self.request_uri_ttl))
                ttl = self._positive_number(ttl, "ttl")
                created_at = self._request_time(params, nested)
                request_uri = _REQUEST_URI_PREFIX + secrets.token_urlsafe(32)
                while request_uri in state["request_uris"]:
                    request_uri = _REQUEST_URI_PREFIX + secrets.token_urlsafe(32)

                scope = " ".join(sorted(outer_scope))
                state["request_uris"][request_uri] = {
                    "request_uri": request_uri,
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": scope,
                    "code_challenge_method": challenge_method,
                    "created_at": created_at,
                    "expires_at": created_at + ttl,
                    "status": "pending",
                    "pushed_request": _json_safe(request),
                }
                self._audit(
                    state,
                    "push",
                    "accepted",
                    request_uri=request_uri,
                    client_id=client_id,
                    scope=scope,
                )
                return request_uri
            except ParError as exc:
                self._audit(
                    state,
                    "push",
                    "rejected",
                    exc.code,
                    client_id=params.get("client_id") if "params" in locals() else None,
                )
                raise
            except ValueError as exc:
                self._audit(state, "push", "rejected", "invalid_request")
                raise ParError("invalid_request", str(exc)) from exc

        return self._execute(operation)

    def authorize(self, request_uri, client_id, redirect_uri=None, now=0.0):
        """Consume a request URI and atomically issue one authorization code."""

        def operation(state):
            record = state["request_uris"].get(request_uri)
            if record is None:
                self._audit(
                    state,
                    "authorize",
                    "rejected",
                    "invalid_request_uri",
                    request_uri=request_uri,
                    client_id=client_id,
                )
                raise ParError("invalid_request_uri", "unknown request URI")

            if record.get("status") != "pending":
                self._audit(
                    state,
                    "authorize",
                    "rejected",
                    "invalid_request_uri",
                    request_uri=request_uri,
                    client_id=client_id,
                    status=record.get("status"),
                )
                raise ParError("invalid_request_uri", "request URI is no longer usable")

            try:
                current_time = float(now)
            except (TypeError, ValueError) as exc:
                raise ParError("invalid_request", "invalid authorization time") from exc

            if current_time >= float(record["expires_at"]):
                record["status"] = "expired"
                record["expired_at"] = current_time
                self._audit(
                    state,
                    "authorize",
                    "rejected",
                    "invalid_request_uri",
                    request_uri=request_uri,
                    client_id=client_id,
                    reason="expired",
                )
                raise ParError("invalid_request_uri", "request URI has expired")

            if client_id != record.get("client_id"):
                self._audit(
                    state,
                    "authorize",
                    "rejected",
                    "invalid_client",
                    request_uri=request_uri,
                    client_id=client_id,
                    bound_client_id=record.get("client_id"),
                )
                raise ParError("invalid_client", "request URI is bound to another client")

            if redirect_uri != record.get("redirect_uri"):
                self._audit(
                    state,
                    "authorize",
                    "rejected",
                    "invalid_request",
                    request_uri=request_uri,
                    client_id=client_id,
                    reason="redirect_uri_mismatch",
                )
                raise ParError("invalid_request", "redirect_uri does not match")

            code = "code-" + secrets.token_urlsafe(32)
            while code in state["codes"]:
                code = "code-" + secrets.token_urlsafe(32)

            record["status"] = "consumed"
            record["consumed_at"] = current_time
            record["code"] = code
            state["codes"][code] = {
                "code": code,
                "client_id": record["client_id"],
                "request_uri": request_uri,
                "redirect_uri": record["redirect_uri"],
                "scope": record.get("scope", ""),
                "created_at": current_time,
                "status": "issued",
            }
            self._audit(
                state,
                "authorize",
                "accepted",
                request_uri=request_uri,
                client_id=client_id,
                code=code,
            )
            return {
                "code": code,
                "scope": record.get("scope", ""),
                "request_uri": request_uri,
            }

        return self._execute(operation)

    def exchange(self, code, client_id, method="S256", code_verifier=None):
        """Exchange an authorization code, enforcing the configured PKCE policy."""

        def operation(state):
            if self.require_pkce_s256 and method != "S256":
                self._audit(
                    state,
                    "exchange",
                    "rejected",
                    "invalid_request",
                    client_id=client_id,
                    reason="pkce_method",
                )
                raise ParError("invalid_request", "S256 PKCE is required")

            record = state["codes"].get(code)
            if record is None:
                self._audit(
                    state,
                    "exchange",
                    "rejected",
                    "invalid_grant",
                    client_id=client_id,
                )
                raise ParError("invalid_grant", "unknown authorization code")

            if record.get("client_id") != client_id or record.get("status") != "issued":
                self._audit(
                    state,
                    "exchange",
                    "rejected",
                    "invalid_grant",
                    client_id=client_id,
                    code=code,
                )
                raise ParError("invalid_grant", "authorization code is not valid")

            record["status"] = "consumed"
            record["consumed_at"] = time.time()
            token = "at-" + secrets.token_urlsafe(32)
            self._audit(
                state,
                "exchange",
                "accepted",
                client_id=client_id,
                code=code,
            )
            return {
                "access_token": token,
                "token_type": "Bearer",
                "scope": record.get("scope", ""),
            }

        return self._execute(operation)

    def pending_count(self):
        """Return the number of request URIs that have not yet been consumed."""

        def operation(state):
            return sum(
                1
                for record in state["request_uris"].values()
                if record.get("status") == "pending"
            )

        return self._execute(operation)

    def audit_records(self):
        """Return a copy of the persistent audit trail."""

        def operation(state):
            return copy.deepcopy(state["audit"])

        return self._execute(operation)

    def pushed_records(self):
        """Return a copy of all pushed request records, including consumed ones."""

        def operation(state):
            return copy.deepcopy(state["request_uris"])

        return self._execute(operation)
