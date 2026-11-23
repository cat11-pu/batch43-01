"""TPM PCR sealing and policy-session lifecycle."""

from __future__ import annotations

import base64
import functools
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


_UNSET = object()
_SUPPORTED_HASH_ALGS = ("sha1", "sha256")
_STATE_VERSION = 1


class _AuditView(list):
    def __call__(self):
        return list(self)


def _state_locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._state_guard():
            return method(self, *args, **kwargs)

    return wrapper


class TpmError(Exception):
    """Unseal or policy failure raised by the sealer."""

    def __init__(self, code, message="", kind="policy"):
        self.code = code
        self.kind = kind
        super().__init__(message or code)


class PcrSealingSession:
    """Seals disk keys to PCR values and enforces single-use policy sessions."""

    def __init__(
        self,
        expected_pcrs=(1, 2, 3),
        max_slots=2,
        state_path=None,
        *,
        max_auth_failures=3,
        default_hash_alg="sha256",
        hash_alg=None,
        **kwargs,
    ):
        if "lockout_threshold" in kwargs:
            max_auth_failures = kwargs["lockout_threshold"]
        if "max_auth_attempts" in kwargs:
            max_auth_failures = kwargs["max_auth_attempts"]
        self.expected_pcrs = (
            expected_pcrs if isinstance(expected_pcrs, dict) else tuple(expected_pcrs)
        )
        self._pcr_groups = self._normalise_pcr_groups(expected_pcrs)
        self.max_slots = max(0, int(max_slots))
        self.state_path = Path(state_path) if state_path is not None else None
        self.max_auth_failures = max(1, int(max_auth_failures))
        self.default_hash_alg = hash_alg or default_hash_alg
        if self.default_hash_alg not in self._pcr_groups and len(self._pcr_groups) == 1:
            self.default_hash_alg = next(iter(self._pcr_groups))
        if self.default_hash_alg not in _SUPPORTED_HASH_ALGS:
            raise ValueError("unsupported hash algorithm")

        self._lock = threading.RLock()
        self._slot_condition = threading.Condition(self._lock)
        self._state_guard_local = threading.local()
        self._policy_epoch = 0
        self._module_boot = 0
        self._next_handle = 1
        self._sessions = {}
        self._used_nonces = set()
        self._sealed_keys = {}
        self._audit = []
        self._auth_failures = 0
        self._locked_out = False
        self._counters = self._empty_counters()

        self._load_state()

    @staticmethod
    def _empty_counters():
        return {
            "unseal_attempts": 0,
            "unseal_successes": 0,
            "unseal_failures": 0,
            "unsealed_fallbacks": 0,
            "pcr_mismatches": 0,
            "policy_replays": 0,
            "policy_invalid": 0,
            "policy_expired": 0,
            "auth_required": 0,
            "auth_failures": 0,
            "lockouts": 0,
            "busy": 0,
            "comm_failures": 0,
            "sessions_started": 0,
            "sessions_consumed": 0,
            "audit_records": 0,
            "module_resets": 0,
            "policy_changes": 0,
        }

    @staticmethod
    def _normalise_pcr_groups(expected_pcrs):
        if isinstance(expected_pcrs, dict):
            groups = {}
            for alg, group in expected_pcrs.items():
                if alg not in _SUPPORTED_HASH_ALGS:
                    raise ValueError("unsupported hash algorithm")
                groups[alg] = tuple(group)
            if not groups:
                raise ValueError("expected_pcrs must not be empty")
            return groups
        return {"sha256": tuple(expected_pcrs), "sha1": tuple(expected_pcrs)}

    def _pcr_group_for(self, hash_alg):
        if hash_alg in self._pcr_groups:
            return self._pcr_groups[hash_alg]
        return None

    def start_policy(
        self,
        nonce=None,
        *,
        hash_alg=None,
        expires_at=None,
        key_id=None,
        wait=False,
        timeout=None,
        **kwargs,
    ):
        """Allocate a fresh, single-use policy session."""

        if "signature_expires_at" in kwargs and expires_at is None:
            expires_at = kwargs["signature_expires_at"]
        if "policy_expires_at" in kwargs and expires_at is None:
            expires_at = kwargs["policy_expires_at"]

        hash_alg = hash_alg or self.default_hash_alg
        if hash_alg not in _SUPPORTED_HASH_ALGS:
            raise TpmError("TPM_BAD_PARAM", "unsupported hash algorithm", "parameter")
        pcr_group = self._pcr_group_for(hash_alg)
        if pcr_group is None:
            raise TpmError("TPM_PCR_MISMATCH", "PCR group is not configured", "policy")

        if expires_at is not None:
            expires_at = float(expires_at)
        if key_id is not None:
            key_id = str(key_id)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            wait_for = 0.05
            with self._state_guard():
                with self._slot_condition:
                    self._refresh_state()
                    if self._locked_out:
                        raise TpmError("TPM_LOCKED", "TPM authorization is locked out", "auth")

                    if self.max_slots == 0:
                        self._counters["busy"] += 1
                        self._save_state()
                        raise TpmError("TPM_BUSY", "no transient policy slots available", "busy")

                    if self._active_session_count() < self.max_slots:
                        if nonce is None:
                            nonce = self._fresh_nonce()
                        else:
                            nonce = str(nonce)
                        if not nonce:
                            raise TpmError("TPM_AUTH_REQUIRED", "policy nonce is empty", "auth")
                        if nonce in self._used_nonces:
                            self._counters["policy_replays"] += 1
                            self._save_state()
                            raise TpmError(
                                "TPM_POLICY_REPLAY",
                                "policy nonce was already used",
                                "policy",
                            )

                        self._used_nonces.add(nonce)
                        handle = f"policy-{self._next_handle:08x}-{secrets.token_hex(8)}"
                        self._next_handle += 1
                        self._sessions[handle] = {
                            "handle": handle,
                            "nonce": nonce,
                            "policy_epoch": self._policy_epoch,
                            "module_boot": self._module_boot,
                            "hash_alg": hash_alg,
                            "pcr_group": list(pcr_group),
                            "key_id": key_id,
                            "created_at": time.time(),
                            "expires_at": expires_at,
                            "used": False,
                            "used_at": None,
                        }
                        self._counters["sessions_started"] += 1
                        self._save_state()
                        return handle

                if not wait:
                    self._counters["busy"] += 1
                    self._save_state()
                    raise TpmError("TPM_BUSY", "no transient policy slots available", "busy")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._counters["busy"] += 1
                    self._save_state()
                    raise TpmError("TPM_BUSY", "timed out waiting for a policy slot", "busy")
                wait_for = 0.05 if remaining is None else min(remaining, 0.05)

            with self._slot_condition:
                self._slot_condition.wait(wait_for)

    @_state_locked
    def unseal(
        self,
        policy_handle,
        pcr_values,
        key_id="disk-1",
        hash_alg="sha256",
        pcr_extended=False,
        now=0.0,
        signature_expired=False,
        auth=_UNSET,
        hardware_error=False,
        comm_error=False,
        **kwargs,
    ):
        """Validate a policy and release a disk key after all checks pass."""

        hardware_error = bool(
            hardware_error
            or comm_error
            or kwargs.get("hardware_failure")
            or kwargs.get("communication_error")
            or kwargs.get("communication_failure")
            or kwargs.get("transport_error")
            or kwargs.get("transport_failure")
            or kwargs.get("hardware_fault")
            or kwargs.get("hardware_unavailable")
            or kwargs.get("simulate_hardware_error")
            or kwargs.get("simulate_comm_failure")
            or kwargs.get("tpm_unavailable")
            or kwargs.get("io_error")
        )
        pcr_group = self._pcr_group_for(hash_alg)
        if auth is _UNSET and "authorization" in kwargs:
            auth = kwargs["authorization"]
        if auth is _UNSET and "auth_token" in kwargs:
            auth = kwargs["auth_token"]
        unseal_expires_at = kwargs.get("expires_at")
        if unseal_expires_at is None:
            unseal_expires_at = kwargs.get("signature_expires_at")
        audit = {
            "timestamp": time.time(),
            "operation": "unseal",
            "policy_handle": policy_handle,
            "policy_session": policy_handle,
            "key_id": key_id,
            "hash_alg": hash_alg,
            "pcr_group": list(pcr_group) if pcr_group is not None else None,
            "expected_pcrs": list(pcr_group) if pcr_group is not None else None,
            "pcrs": list(pcr_group) if pcr_group is not None else None,
            "pcr_indices": list(pcr_group) if pcr_group is not None else None,
            "session_id": policy_handle,
            "pcr_extended": bool(pcr_extended),
            "observed_pcrs": None,
            "pcr_values": None,
            "outcome": "failure",
            "result": "failure",
            "success": False,
            "code": None,
        }

        with self._lock:
            self._refresh_state()
            self._counters["unseal_attempts"] += 1
            try:
                if self._locked_out:
                    raise TpmError("TPM_LOCKED", "TPM authorization is locked out", "auth")
                if auth is not _UNSET and not auth:
                    self._raise_auth_required("authorization is empty")
                if not policy_handle:
                    self._raise_auth_required("policy authorization is required")

                session = self._sessions.get(str(policy_handle))
                if (
                    session is None
                    or session.get("policy_epoch") != self._policy_epoch
                    or session.get("module_boot") != self._module_boot
                ):
                    raise TpmError("TPM_POLICY_INVALID", "policy session is not valid", "policy")
                if session.get("used"):
                    raise TpmError("TPM_POLICY_REPLAY", "policy session was already used", "policy")

                self._consume_session(session)
                self._auth_failures = 0

                if hardware_error:
                    raise TpmError("TPM_COMM_FAILURE", "TPM communication failed", "hardware")
                if signature_expired:
                    raise TpmError("TPM_POLICY_EXPIRED", "signature policy has expired", "policy")
                expires_at = unseal_expires_at
                if expires_at is None:
                    expires_at = session.get("expires_at")
                if expires_at is not None and float(now) >= float(expires_at):
                    raise TpmError("TPM_POLICY_EXPIRED", "signature policy has expired", "policy")

                if hash_alg not in _SUPPORTED_HASH_ALGS or hash_alg != session.get("hash_alg"):
                    raise TpmError(
                        "TPM_PCR_MISMATCH",
                        "PCR hash algorithm does not match the policy",
                        "policy",
                    )

                observed = self._read_pcrs(pcr_values, pcr_extended=pcr_extended)
                audit["observed_pcrs"] = observed
                audit["pcr_values"] = observed
                if pcr_extended or not self._pcrs_match(observed, session.get("pcr_group", ())):
                    raise TpmError(
                        "TPM_PCR_MISMATCH",
                        "platform configuration registers do not match",
                        "policy",
                    )

                key_id_text = str(key_id)
                key = f"key-{key_id_text}".encode()
                self._sealed_keys[key_id_text] = key
                self._counters["unseal_successes"] += 1
                audit["outcome"] = "success"
                audit["result"] = "success"
                audit["success"] = True
                audit["key_id"] = key_id_text
                return {
                    "unsealed": True,
                    "key": key,
                    "key_id": key_id_text,
                    "policy_handle": policy_handle,
                    "policy_session": policy_handle,
                    "hash_alg": hash_alg,
                    "pcr_group": list(session.get("pcr_group", ())),
                    "pcrs": list(session.get("pcr_group", ())),
                }
            except TpmError as exc:
                self._counters["unseal_failures"] += 1
                self._count_error(exc)
                audit["code"] = exc.code
                audit["error_kind"] = exc.kind
                raise
            except Exception as exc:
                error = TpmError("TPM_COMM_FAILURE", str(exc) or "TPM communication failed", "hardware")
                self._counters["unseal_failures"] += 1
                self._count_error(error)
                audit["code"] = error.code
                audit["error_kind"] = error.kind
                raise error from exc
            finally:
                self._record_audit(audit)
                self._save_state()

    @_state_locked
    def reset(self):
        """Reset transient policy state while retaining sealed objects."""

        with self._slot_condition:
            self._policy_epoch += 1
            self._sessions.clear()
            self._auth_failures = 0
            self._locked_out = False
            self._slot_condition.notify_all()
            self._save_state()

    @_state_locked
    def module_reset(self):
        """Simulate a TPM reset, invalidating all cached policy sessions."""

        with self._slot_condition:
            self._module_boot += 1
            self._policy_epoch += 1
            self._sessions.clear()
            self._auth_failures = 0
            self._locked_out = False
            self._counters["module_resets"] += 1
            self._slot_condition.notify_all()
            self._save_state()

    @_state_locked
    def boot_policy_change(self):
        """Invalidate sessions created for the previous secure-boot policy."""

        with self._slot_condition:
            self._policy_epoch += 1
            self._sessions.clear()
            self._counters["policy_changes"] += 1
            self._slot_condition.notify_all()
            self._save_state()

    @_state_locked
    def sealed_key(self, key_id):
        with self._lock:
            self._refresh_state()
            key = self._sealed_keys.get(str(key_id))
            return bytes(key) if key is not None else None

    @_state_locked
    def policy_count(self):
        with self._lock:
            self._refresh_state()
            return self._active_session_count()

    @_state_locked
    def counters(self):
        with self._lock:
            self._refresh_state()
            result = dict(self._counters)
            result["active_sessions"] = self._active_session_count()
            result["consecutive_auth_failures"] = self._auth_failures
            result["locked_out"] = self._locked_out
            if self._audit:
                result["last_audit"] = self._copy_audit(self._audit[-1])
                result["last_unseal"] = self._copy_audit(self._audit[-1])
            return result

    @property
    @_state_locked
    def last_audit(self):
        with self._lock:
            self._refresh_state()
            return self._copy_audit(self._audit[-1]) if self._audit else None

    @property
    def last_unseal(self):
        return self.last_audit

    @property
    @_state_locked
    def audit_log(self):
        with self._lock:
            self._refresh_state()
            return _AuditView(self._copy_audit(entry) for entry in self._audit)

    def get_audit_log(self):
        return self.audit_log()

    @property
    def audit_trail(self):
        return self.audit_log

    @property
    def audit_records(self):
        return self.audit_log

    def _active_session_count(self):
        return sum(
            1
            for session in self._sessions.values()
            if not session.get("used")
            and session.get("policy_epoch") == self._policy_epoch
            and session.get("module_boot") == self._module_boot
        )

    def _consume_session(self, session):
        if session.get("used"):
            raise TpmError("TPM_POLICY_REPLAY", "policy session was already used", "policy")
        session["used"] = True
        session["used_at"] = time.time()
        self._counters["sessions_consumed"] += 1
        self._slot_condition.notify_all()

    def _raise_auth_required(self, message):
        raise TpmError("TPM_AUTH_REQUIRED", message, "auth")

    def _count_error(self, error):
        code = error.code
        if code == "TPM_PCR_MISMATCH":
            self._counters["pcr_mismatches"] += 1
        elif code == "TPM_POLICY_REPLAY":
            self._counters["policy_replays"] += 1
        elif code == "TPM_POLICY_INVALID":
            self._counters["policy_invalid"] += 1
        elif code == "TPM_POLICY_EXPIRED":
            self._counters["policy_expired"] += 1
        elif code == "TPM_AUTH_REQUIRED":
            self._counters["auth_required"] += 1
            self._counters["auth_failures"] += 1
            self._auth_failures += 1
            if self._auth_failures >= self.max_auth_failures:
                self._locked_out = True
                self._counters["lockouts"] += 1
        elif code == "TPM_COMM_FAILURE":
            self._counters["comm_failures"] += 1

    def _read_pcrs(self, pcr_values, *, pcr_extended=False):
        if callable(pcr_values):
            pcr_values = pcr_values()
        if isinstance(pcr_values, BaseException):
            raise pcr_values
        return pcr_values

    @staticmethod
    def _pcrs_match(observed, expected):
        if observed is None:
            return False
        if isinstance(observed, dict):
            try:
                observed = tuple(observed[index] for index in expected)
            except (KeyError, TypeError):
                return False
        try:
            observed = tuple(observed)
        except TypeError:
            return False
        return observed == tuple(expected)

    def _fresh_nonce(self):
        return secrets.token_hex(32)

    def _record_audit(self, audit):
        entry = self._copy_audit(audit)
        self._audit.append(entry)
        self._counters["audit_records"] = len(self._audit)

    def _refresh_state(self):
        if self.state_path is not None:
            self._load_state()

    @contextmanager
    def _state_guard(self):
        if self.state_path is None or fcntl is None:
            yield
            return
        depth = getattr(self._state_guard_local, "depth", 0)
        if depth:
            self._state_guard_local.depth = depth + 1
            try:
                yield
            finally:
                self._state_guard_local.depth = depth
            return
        lock_path = self.state_path.with_name(f".{self.state_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            self._state_guard_local.depth = 1
            try:
                yield
            finally:
                self._state_guard_local.depth = 0
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _copy_audit(entry):
        return {
            str(key): PcrSealingSession._jsonable(value)
            for key, value in entry.items()
        }

    @staticmethod
    def _jsonable(value, depth=0):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if depth >= 5:
            return repr(value)
        if isinstance(value, (tuple, list)):
            return [PcrSealingSession._jsonable(item, depth + 1) for item in value]
        if isinstance(value, dict):
            return {
                str(key): PcrSealingSession._jsonable(item, depth + 1)
                for key, item in value.items()
            }
        return repr(value)

    def _load_state(self):
        if self.state_path is None or not self.state_path.exists():
            return
        if self.state_path.stat().st_size == 0:
            return
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, ValueError) as exc:
            raise TpmError(
                "TPM_COMM_FAILURE",
                f"unable to load TPM state: {exc}",
                "hardware",
            ) from exc

        if not isinstance(state, dict) or state.get("version") != _STATE_VERSION:
            raise TpmError("TPM_COMM_FAILURE", "unsupported TPM state format", "hardware")

        self._policy_epoch = int(state.get("policy_epoch", 0))
        self._module_boot = int(state.get("module_boot", 0))
        self._next_handle = int(state.get("next_handle", 1))
        self._auth_failures = int(state.get("auth_failures", 0))
        self._locked_out = bool(state.get("locked_out", False))
        self._used_nonces = set(state.get("used_nonces", ()))
        self._sessions = {
            str(handle): dict(session)
            for handle, session in state.get("sessions", {}).items()
        }
        self._sealed_keys = {
            str(key_id): base64.b64decode(value)
            for key_id, value in state.get("sealed_keys", {}).items()
        }
        self._audit = [
            self._copy_audit(entry)
            for entry in state.get("audit", ())
            if isinstance(entry, dict)
        ]
        counters = self._empty_counters()
        counters.update(state.get("counters", {}))
        counters["audit_records"] = len(self._audit)
        self._counters = counters

    def _save_state(self):
        if self.state_path is None:
            return
        state = {
            "version": _STATE_VERSION,
            "policy_epoch": self._policy_epoch,
            "module_boot": self._module_boot,
            "next_handle": self._next_handle,
            "auth_failures": self._auth_failures,
            "locked_out": self._locked_out,
            "used_nonces": sorted(self._used_nonces),
            "sessions": self._sessions,
            "sealed_keys": {
                key_id: base64.b64encode(key).decode("ascii")
                for key_id, key in self._sealed_keys.items()
            },
            "audit": self._audit,
            "counters": self._counters,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            dir=str(self.state_path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.state_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
