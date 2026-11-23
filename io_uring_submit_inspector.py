"""io_uring submission inspector.

The real kernel would never be reached by this module.  It models the
security-relevant admission checks around an io_uring submission queue so a
submission can be rejected before it has any side effects.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import unicodedata


class IouringError(Exception):
    """Configuration failure raised by the inspector."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class IoUringSubmitInspector:
    """Validates io_uring submissions before they reach the kernel."""

    _TASK_KEYS = ("build_task_id", "build_task", "task_id", "task", "job_id", "job")
    _IDENTITY_KEYS = ("identity", "subject", "principal", "auth_id", "build_identity")
    _CREDENTIAL_KEYS = ("credentials", "credential", "capabilities")
    _BUFFER_INDEX_KEYS = ("buffer_index", "buf_index", "buffer_id")
    _BUFFER_ADDRESS_KEYS = (
        "user_address",
        "user_addr",
        "buffer_address",
        "buffer_addr",
        "buf_address",
        "buf_addr",
        "address",
        "addr",
        "iov_base",
    )
    _BUFFER_LENGTH_KEYS = (
        "buffer_length",
        "buffer_len",
        "buf_length",
        "buf_len",
        "user_length",
        "user_len",
        "length",
        "len",
        "iov_len",
    )
    _FIXED_FILE_INDEX_KEYS = ("fixed_file_index", "file_index", "fixed_file_id")
    _RING_DEPTH_KEYS = (
        "ring_depth",
        "ring_size",
        "ring_entries",
        "sq_entries",
        "cq_entries",
        "sq_depth",
        "cq_depth",
        "mmap_entries",
    )
    _PATH_KEYS = ("path", "file", "filename", "file_path", "fixed_file_path")

    _SAFE_FLAGS = {
        "IOSQE_FIXED_FILE",
        "IOSQE_IO_DRAIN",
        "IOSQE_IO_LINK",
        "IOSQE_IO_HARDLINK",
        "IOSQE_BUFFER_SELECT",
        "IOSQE_CQE_SKIP_SUCCESS",
    }
    _DENIED_FLAGS = {
        "IOSQE_ASYNC",
        "IORING_SETUP_SQPOLL",
        "IORING_SETUP_IOPOLL",
        "IORING_SETUP_SQ_AFF",
        "IORING_SETUP_ATTACH_WQ",
    }
    _IOSQE_ASYNC_BIT = 1 << 4
    _KNOWN_IOSQE_BITS = 0x7F

    def __init__(
        self,
        allowed_opcodes=("READ", "WRITE", "TIMEOUT"),
        queue_depth=8,
        allowed_root="/sandbox",
        max_fixed_files=4,
        **config,
    ):
        self.allowed_opcodes = frozenset(
            str(opcode).strip().upper() for opcode in allowed_opcodes
        )
        if isinstance(queue_depth, bool) or not isinstance(queue_depth, int) or queue_depth <= 0:
            raise IouringError("EINVAL", "queue_depth must be a positive integer")
        self.queue_depth = queue_depth
        self.allowed_root = self._normalize_root(allowed_root)
        if (
            isinstance(max_fixed_files, bool)
            or not isinstance(max_fixed_files, int)
            or max_fixed_files < 0
        ):
            raise IouringError("EINVAL", "max_fixed_files must be non-negative")
        self.max_fixed_files = max_fixed_files

        constructor_depth_keys = (
            "ring_depth",
            "ring_size",
            "ring_entries",
            "sq_entries",
            "cq_entries",
            "sq_depth",
            "cq_depth",
            "mmap_entries",
        )
        for key in constructor_depth_keys:
            if key in config and config[key] != self.queue_depth:
                raise IouringError(
                    "EINVAL", "ring mapping depth does not match queue depth"
                )
        if config.get("sqpoll") or config.get("sq_thread") or config.get("iopoll"):
            raise IouringError("EPERM", "kernel polling threads are not permitted")

        self._lock = threading.RLock()
        self._buffers = []
        self._fixed_files = []
        self._identity = None
        self._credentials = None
        self._closed = False

        self._submitted = 0
        self._accepted = 0
        self._executed = 0
        self._rejected = 0
        self._cancelled = 0
        self._freed_files = 0
        self._default_task = config.get("task_id", config.get("build_task_id"))
        self._released_files = []
        self._completions = []
        self._inflight = []
        self._audit = []

    # ------------------------------------------------------------------
    # Registration

    def register_buffers(self, buffers):
        """Register user buffers without dereferencing any supplied address."""
        with self._lock:
            self._ensure_open()
            descriptors = self._parse_buffers(buffers)
            self._buffers = descriptors
            return {"ok": True, "registered": len(descriptors)}

    def register_files(self, paths):
        """Atomically replace the ring's fixed-file table after path checks."""
        with self._lock:
            self._ensure_open()
            if isinstance(paths, (str, bytes, os.PathLike)):
                paths = [paths]
            try:
                paths = list(paths)
            except TypeError as exc:
                raise IouringError("EINVAL", "paths must be iterable") from exc
            if len(paths) > self.max_fixed_files:
                raise IouringError("EMFILE", "fixed-file table limit exceeded")

            normalized = [self._validate_path(path) for path in paths]
            released = len(self._fixed_files)
            if released:
                self._released_files.extend(self._fixed_files)
                self._freed_files += released
            self._fixed_files = normalized
            return {"ok": True, "registered": len(normalized), "released": released}

    def register_identity(self, identity=None, **credentials):
        """Bind the ring to one immutable identity and credential set."""
        with self._lock:
            self._ensure_open()
            descriptor = identity if isinstance(identity, dict) else {}
            if descriptor:
                identity = descriptor.get(
                    "identity",
                    descriptor.get("id", descriptor.get("subject")),
                )
                for key, value in descriptor.items():
                    if key not in ("identity", "id", "subject"):
                        credentials.setdefault(key, value)

            canonical = self._canonical_credentials(credentials)
            if self._identity is not None:
                if identity != self._identity or canonical != self._credentials:
                    raise IouringError("EPERM", "registered identity cannot be replaced")
                return {"ok": True, "identity": self._identity, "unchanged": True}

            self._identity = identity
            self._credentials = canonical
            return {"ok": True, "identity": identity}

    # ------------------------------------------------------------------
    # Submission and completion

    def submit(self, request):
        """Preflight and submit one request or one atomically linked chain."""
        with self._lock:
            if self._closed:
                task = self._task_name(request)
                opcode = self._opcode_name(request)
                return self._reject("EBADF", opcode, task)
            try:
                batch = self._flatten_batch(request)
            except IouringError as exc:
                return self._reject(exc.code, "UNKNOWN", self._task_name(None))
            self._submitted += len(batch)
            if not batch:
                return self._reject("EINVAL", "UNKNOWN", self._task_name(None))

            root = batch[0]
            fallback_task = self._task_name(root)
            proposed_identity = self._identity
            proposed_credentials = self._credentials

            for index, item in enumerate(batch):
                opcode = self._opcode_name(item)
                task = self._explicit_task_name(item) or fallback_task
                try:
                    proposed_identity, proposed_credentials = self._preflight_item(
                        item,
                        index,
                        batch,
                        proposed_identity,
                        proposed_credentials,
                    )
                except IouringError as exc:
                    return self._reject(exc.code, opcode, task, index=index)
                except (TypeError, ValueError, OverflowError):
                    return self._reject("EINVAL", opcode, task, index=index)

            if self._queue_used() + len(batch) > self.queue_depth:
                return self._reject(
                    "EOVERFLOW",
                    self._opcode_name(root),
                    fallback_task,
                )

            if self._identity is None and proposed_identity is not None:
                self._identity = proposed_identity
                self._credentials = proposed_credentials

            opcodes = [self._opcode_name(item) for item in batch]
            if any(self._is_inflight(item) for item in batch):
                self._inflight.extend(opcodes)
            else:
                self._completions.extend(opcodes)
            self._executed += len(batch)
            self._accepted += len(batch)
            return {
                "ok": True,
                "error": None,
                "executed": len(batch),
                "opcodes": opcodes,
            }

    def close(self):
        """Cancel in-flight work and release each fixed file exactly once."""
        with self._lock:
            if self._closed:
                return {
                    "ok": True,
                    "cancelled": 0,
                    "freed_files": 0,
                    "already_closed": True,
                }

            self._closed = True
            cancelled = len(self._inflight)
            self._inflight.clear()
            files = self._fixed_files
            self._fixed_files = []
            self._released_files.extend(files)
            self._freed_files += len(files)
            self._cancelled += cancelled
            return {
                "ok": True,
                "cancelled": cancelled,
                "freed_files": len(files),
                "released_files": len(files),
                "already_closed": False,
            }

    def completions(self):
        with self._lock:
            return list(self._completions)

    def completion_events(self):
        with self._lock:
            return [
                {"opcode": opcode, "sequence": sequence}
                for sequence, opcode in enumerate(self._completions)
            ]

    # ------------------------------------------------------------------
    # Introspection

    def stats(self):
        with self._lock:
            audit = copy.deepcopy(self._audit)
            return {
                "submitted": self._submitted,
                "accepted": self._accepted,
                "executed": self._executed,
                "rejected": self._rejected,
                "cancelled": self._cancelled,
                "freed_files": self._freed_files,
                "in_flight": len(self._inflight),
                "queue_depth": self.queue_depth,
                "completions": len(self._completions),
                "registered_buffers": len(self._buffers),
                "fixed_files": len(self._fixed_files),
                "fixed_file_paths": list(self._fixed_files),
                "released_files": list(self._released_files),
                "closed": self._closed,
                "identity": self._identity,
                "audit": audit,
                "audit_log": audit,
                "rejections": copy.deepcopy(audit),
            }

    def audit_log(self):
        with self._lock:
            return copy.deepcopy(self._audit)

    def audit_events(self):
        return self.audit_log()

    # Compatibility aliases for callers that use the kernel naming.
    def close_ring(self):
        return self.close()

    def bind_identity(self, identity=None, **credentials):
        return self.register_identity(identity, **credentials)

    # ------------------------------------------------------------------
    # Preflight

    def _preflight_item(self, item, index, batch, identity, credentials):
        if not isinstance(item, dict):
            raise IouringError("EINVAL", "request must be a mapping")

        opcode = self._opcode_name(item)
        if opcode not in self.allowed_opcodes:
            raise IouringError("EPERM", "opcode is not allowlisted")

        self._check_flags(item)
        self._check_ring_depth(item)
        identity, credentials = self._check_identity(item, identity, credentials)
        self._check_paths(item)
        self._check_fixed_file(item)
        self._check_buffers(item)
        self._check_timeout(item, index, batch)
        return identity, credentials

    def _check_flags(self, request):
        for field in ("flags", "sqe_flags", "setup_flags", "io_flags"):
            if field in request and request[field] is not None:
                for token in self._flag_tokens(request[field]):
                    normalized = token.strip().upper()
                    if not normalized:
                        continue
                    if normalized in self._DENIED_FLAGS:
                        raise IouringError("EPERM", "flag bypasses inspection")
                    if normalized not in self._SAFE_FLAGS:
                        raise IouringError("EPERM", "unknown or unsafe submission flag")

        for field in (
            "async",
            "async_flag",
            "async_submit",
            "force_async",
            "offload",
            "worker_offload",
            "sq_thread",
            "sqpoll",
            "sq_poll",
            "kernel_poll",
            "polling_thread",
            "kernel_thread",
            "iopoll",
            "io_poll",
        ):
            if request.get(field):
                raise IouringError("EPERM", "asynchronous dispatch is not permitted")

    def _check_ring_depth(self, request):
        for field in self._RING_DEPTH_KEYS:
            if field not in request or request[field] is None:
                continue
            value = request[field]
            if isinstance(value, bool) or not isinstance(value, int):
                raise IouringError("EINVAL", "ring depth must be an integer")
            if value != self.queue_depth:
                raise IouringError("EINVAL", "ring depth does not match queue depth")

        for field in ("ring_depth", "ring_size", "ring_entries", "sq_entries", "cq_entries"):
            if field in request and request[field] is None:
                raise IouringError("EINVAL", "ring depth cannot be unspecified")

    def _check_identity(self, request, identity, credentials):
        identity_values = [request[key] for key in self._IDENTITY_KEYS if key in request]
        request_identity_present = bool(identity_values)
        request_identity = identity_values[0] if identity_values else None
        if any(value != request_identity for value in identity_values[1:]):
            raise IouringError("EPERM", "conflicting request identities")

        request_credentials = {}
        for key in self._CREDENTIAL_KEYS:
            if key in request:
                request_credentials[key] = request[key]
        for key in ("uid", "gid", "groups", "token", "capability"):
            if key in request:
                request_credentials[key] = request[key]
        canonical_credentials = (
            self._canonical_credentials(request_credentials)
            if request_credentials
            else None
        )

        if identity is not None:
            if request_identity_present and request_identity != identity:
                raise IouringError("EPERM", "request identity differs from registered identity")
            if (
                canonical_credentials is not None
                and credentials is not None
                and canonical_credentials != credentials
            ):
                raise IouringError("EPERM", "request credentials cannot replace registered credentials")
            return identity, credentials

        if request_identity_present or canonical_credentials is not None:
            return request_identity, canonical_credentials
        return identity, credentials

    def _check_paths(self, request):
        for field in self._PATH_KEYS:
            if field in request and request[field] is not None:
                self._validate_path(request[field])

    def _check_fixed_file(self, request):
        table_owner = request.get("fixed_file_table_id", request.get("table_id"))
        if table_owner is not None and table_owner != id(self):
            raise IouringError("EINVAL", "fixed-file table belongs to another ring")

        selected = []
        if "fixed_file" in request and request["fixed_file"] is not None:
            fixed_file = request["fixed_file"]
            if isinstance(fixed_file, bool):
                raise IouringError("EFAULT", "fixed-file index is out of range")
            if isinstance(fixed_file, int) and not isinstance(fixed_file, bool):
                index = self._non_negative_int(fixed_file)
                if index is None or index >= len(self._fixed_files):
                    raise IouringError("EFAULT", "fixed-file index is out of range")
                selected.append(index)
            else:
                self._validate_path(fixed_file)
                if self._normalize_path(fixed_file) not in self._fixed_files:
                    raise IouringError("EPERM", "fixed file is not registered")

        for field in self._FIXED_FILE_INDEX_KEYS:
            if field not in request or request[field] is None:
                continue
            index = self._non_negative_int(request[field])
            if index is None or index >= len(self._fixed_files):
                raise IouringError("EFAULT", "fixed-file index is out of range")
            selected.append(index)

        if selected:
            if any(index != selected[0] for index in selected[1:]):
                raise IouringError("EINVAL", "conflicting fixed-file indexes")
            self._validate_path(self._fixed_files[selected[0]])
            return selected[0]
        return None

    def _check_buffers(self, request):
        indexes = []
        for field in self._BUFFER_INDEX_KEYS:
            if field not in request or request[field] is None:
                continue
            index = self._non_negative_int(request[field])
            if index is None or index >= len(self._buffers):
                raise IouringError("EFAULT", "registered-buffer index is out of range")
            indexes.append(index)
        if indexes and any(index != indexes[0] for index in indexes[1:]):
            raise IouringError("EFAULT", "conflicting registered-buffer indexes")
        descriptor = self._buffers[indexes[0]] if indexes else None

        addresses = []
        for field in self._BUFFER_ADDRESS_KEYS:
            if field not in request or request[field] is None:
                continue
            address = self._non_negative_int(request[field])
            if address is None:
                raise IouringError("EFAULT", "invalid user buffer address")
            addresses.append(address)
        if addresses and any(address != addresses[0] for address in addresses[1:]):
            raise IouringError("EFAULT", "conflicting user buffer addresses")
        address = addresses[0] if addresses else None

        lengths = []
        for field in self._BUFFER_LENGTH_KEYS:
            if field not in request or request[field] is None:
                continue
            length = self._non_negative_int(request[field])
            if length is None:
                raise IouringError("EFAULT", "invalid user buffer length")
            lengths.append(length)
        if lengths and any(length != lengths[0] for length in lengths[1:]):
            raise IouringError("EFAULT", "conflicting user buffer lengths")
        length = lengths[0] if lengths else None

        if indexes and lengths:
            if descriptor is None or length > descriptor["length"]:
                raise IouringError("EFAULT", "buffer range exceeds registered buffer")

        if addresses:
            if length is None:
                length = 1
            end = address + length
            if end < address:
                raise IouringError("EFAULT", "user buffer range overflows")

            candidates = [descriptor] if descriptor is not None else self._buffers
            for candidate in candidates:
                base = candidate.get("address")
                size = candidate["length"]
                if base is not None and base <= address and end <= base + size:
                    return
            raise IouringError("EFAULT", "user buffer is outside registered memory")

    def _check_timeout(self, request, index, batch):
        if self._opcode_name(request) != "TIMEOUT":
            return

        target_index = None
        if "target_index" in request and request["target_index"] is not None:
            target_index = self._non_negative_int(request["target_index"])
            if target_index is None or target_index >= len(batch):
                raise IouringError("EINVAL", "timeout target index is invalid")
        elif "timeout_target" in request:
            timeout_target = request["timeout_target"]
            if isinstance(timeout_target, int) and not isinstance(timeout_target, bool):
                target_index = self._non_negative_int(timeout_target)
                if target_index is None or target_index >= len(batch):
                    raise IouringError("EINVAL", "timeout target index is invalid")
            elif isinstance(timeout_target, str):
                request = dict(request)
                request.setdefault("target_opcode", timeout_target)
            elif isinstance(timeout_target, dict):
                request = dict(request)
                request.setdefault("target", timeout_target)
            else:
                raise IouringError("EINVAL", "timeout target is invalid")

        if target_index is not None and target_index == index:
            raise IouringError("EINVAL", "timeout cannot target itself")

        expected_opcode = request.get("target_opcode")
        if expected_opcode is None:
            target = request.get("target")
            if isinstance(target, dict):
                expected_opcode = target.get("opcode")
                if target_index is None and target.get("index") is not None:
                    target_index = self._non_negative_int(target.get("index"))
                    if target_index is None or target_index >= len(batch):
                        raise IouringError("EINVAL", "timeout target index is invalid")
            elif isinstance(target, str):
                expected_opcode = target
            elif isinstance(target, int):
                target_index = self._non_negative_int(target)
                if target_index is None or target_index >= len(batch):
                    raise IouringError("EINVAL", "timeout target index is invalid")

        if target_index is not None:
            if target_index == index:
                raise IouringError("EINVAL", "timeout cannot target itself")
            if expected_opcode is not None:
                actual = self._opcode_name(batch[target_index])
                if str(expected_opcode).strip().upper() != actual:
                    raise IouringError("EINVAL", "timeout target opcode mismatch")
        elif expected_opcode is not None:
            wanted = str(expected_opcode).strip().upper()
            matching = [
                candidate
                for candidate_index, candidate in enumerate(batch)
                if candidate_index != index and self._opcode_name(candidate) == wanted
            ]
            if not matching:
                matching = [opcode for opcode in self._inflight if opcode == wanted]
            if not matching:
                raise IouringError("EINVAL", "timeout target does not exist")

        if "target_fd" in request or "timeout_fd" in request:
            target_fd = request.get("target_fd", request.get("timeout_fd"))
            if isinstance(target_fd, (str, bytes, os.PathLike)):
                try:
                    normalized = self._normalize_path(target_fd)
                except IouringError as exc:
                    raise IouringError(
                        "EINVAL", "timeout target fd is not registered"
                    ) from exc
                if normalized not in self._fixed_files:
                    raise IouringError("EINVAL", "timeout target fd is not registered")
            else:
                target_fd = self._non_negative_int(target_fd)
                if target_fd is None or target_fd >= len(self._fixed_files):
                    raise IouringError("EINVAL", "timeout target fd is not registered")

    # ------------------------------------------------------------------
    # Validation helpers

    def _parse_buffers(self, buffers):
        if buffers is None:
            return []
        if isinstance(buffers, (bytes, bytearray, memoryview)):
            buffers = [buffers]
        try:
            items = list(buffers)
        except TypeError as exc:
            raise IouringError("EINVAL", "buffers must be iterable") from exc

        descriptors = []
        for item in items:
            address = None
            length = None
            if isinstance(item, (bytes, bytearray, memoryview)):
                length = len(item)
            elif isinstance(item, dict):
                for key in ("address", "addr", "user_address", "base", "iov_base"):
                    if key in item and item[key] is not None:
                        address = self._non_negative_int(item[key])
                        if address is None:
                            raise IouringError("EFAULT", "invalid registered buffer address")
                        break
                for key in ("length", "size", "len", "iov_len"):
                    if key in item and item[key] is not None:
                        length = self._non_negative_int(item[key])
                        if length is None:
                            raise IouringError("EFAULT", "invalid registered buffer length")
                        break
                if length is None:
                    for key in ("buffer", "data", "bytes"):
                        if key in item:
                            value = item[key]
                            if not isinstance(value, (bytes, bytearray, memoryview)):
                                raise IouringError("EFAULT", "invalid registered buffer")
                            length = len(value)
                            break
                else:
                    for key in ("buffer", "data", "bytes"):
                        if key in item:
                            value = item[key]
                            if not isinstance(value, (bytes, bytearray, memoryview)):
                                raise IouringError("EFAULT", "invalid registered buffer")
                            if length > len(value):
                                raise IouringError("EFAULT", "registered buffer length is too large")
                            break
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                address = self._non_negative_int(item[0])
                length = self._non_negative_int(item[1])
                if address is None or length is None:
                    raise IouringError("EFAULT", "invalid registered buffer range")
            else:
                raise IouringError("EINVAL", "unsupported registered buffer descriptor")

            if length is None:
                raise IouringError("EINVAL", "registered buffer length is required")
            if address is not None and address + length < address:
                raise IouringError("EFAULT", "registered buffer range overflows")
            descriptors.append({"address": address, "length": length})
        return descriptors

    def _validate_path(self, path):
        return self._normalize_path(path)

    def _normalize_path(self, path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            raise IouringError("EINVAL", "path must be a string")
        try:
            raw = os.fspath(path)
        except TypeError as exc:
            raise IouringError("EINVAL", "path must be a string") from exc
        if isinstance(raw, bytes):
            try:
                raw = os.fsdecode(raw)
            except (UnicodeError, ValueError) as exc:
                raise IouringError("EPERM", "invalid path encoding") from exc

        raw = unicodedata.normalize("NFC", raw)
        if not raw or "\x00" in raw:
            raise IouringError("EPERM", "path is empty or contains NUL")

        if re.match(r"^[A-Za-z]:[\\/]", raw) or "\\" in raw:
            normalized = self._normalize_windows_path(raw)
        else:
            if not os.path.isabs(raw):
                raise IouringError("EPERM", "path must be absolute")
            normalized = os.path.realpath(os.path.abspath(raw))

        root = self.allowed_root
        try:
            if self._is_windows_path(root):
                common = self._common_windows_path(root, normalized)
            else:
                common = os.path.commonpath([root, normalized])
        except ValueError:
            common = None
        if common is None or self._path_key(common) != self._path_key(root):
            raise IouringError("EPERM", "path escapes allowed root")
        return normalized

    def _normalize_root(self, root):
        if not isinstance(root, (str, bytes, os.PathLike)):
            raise IouringError("EINVAL", "allowed_root must be a path")
        raw = os.fsdecode(os.fspath(root))
        raw = unicodedata.normalize("NFC", raw)
        if not raw or "\x00" in raw:
            raise IouringError("EINVAL", "allowed_root is invalid")
        if re.match(r"^[A-Za-z]:[\\/]", raw) or "\\" in raw:
            normalized = self._normalize_windows_path(raw)
        else:
            normalized = os.path.realpath(os.path.abspath(raw))
        return normalized

    def _normalize_windows_path(self, path):
        import ntpath

        return ntpath.normcase(ntpath.normpath(path))

    def _is_windows_path(self, path):
        return bool(re.match(r"^[A-Za-z]:", path)) or "\\" in path

    def _common_windows_path(self, root, path):
        import ntpath

        return ntpath.commonpath([root, path])

    def _path_key(self, path):
        return path.casefold() if self._is_windows_path(path) else path

    @staticmethod
    def _non_negative_int(value):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 0:
            return None
        return value

    @classmethod
    def _canonical_credentials(cls, credentials):
        if credentials is None:
            return None
        return cls._freeze(credentials)

    @classmethod
    def _freeze(cls, value):
        if isinstance(value, dict):
            return tuple(
                sorted(
                    ((str(key), cls._freeze(item)) for key, item in value.items()),
                    key=lambda item: item[0],
                )
            )
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return tuple(sorted((repr(cls._freeze(item)) for item in value)))
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    @staticmethod
    def _flag_tokens(value):
        if value is None:
            return []
        if isinstance(value, bool):
            return ["IOSQE_ASYNC"] if value else []
        if isinstance(value, int):
            tokens = []
            if value & ~IoUringSubmitInspector._KNOWN_IOSQE_BITS:
                tokens.append("UNKNOWN_FLAG")
            if value & IoUringSubmitInspector._IOSQE_ASYNC_BIT:
                tokens.append("IOSQE_ASYNC")
            return tokens
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        if isinstance(value, str):
            return [token for token in re.split(r"[\s,|]+", value) if token]
        if isinstance(value, dict):
            return [str(key) for key, enabled in value.items() if enabled]
        try:
            tokens = []
            for item in value:
                tokens.extend(IoUringSubmitInspector._flag_tokens(item))
            return tokens
        except TypeError:
            return ["UNKNOWN_FLAG"]

    # ------------------------------------------------------------------
    # Small state helpers

    @staticmethod
    def _flatten_batch(request):
        if not isinstance(request, dict):
            raise IouringError("EINVAL", "request must be a mapping")
        linked = request.get("linked", ())
        if linked is None:
            linked = ()
        elif isinstance(linked, (dict, str)):
            linked = [linked]
        try:
            linked = list(linked)
        except TypeError as exc:
            raise IouringError("EINVAL", "linked requests must be iterable") from exc
        return [request] + linked

    @staticmethod
    def _opcode_name(request):
        if isinstance(request, str):
            value = request
        elif isinstance(request, dict):
            value = request.get("opcode", "UNKNOWN")
        else:
            value = "UNKNOWN"
        if not isinstance(value, str):
            return "UNKNOWN"
        return value.strip().upper() or "UNKNOWN"

    def _task_name(self, request):
        return self._explicit_task_name(request) or str(
            self._default_task or self._identity or "unknown"
        )

    @classmethod
    def _explicit_task_name(cls, request):
        if isinstance(request, dict):
            for key in cls._TASK_KEYS:
                value = request.get(key)
                if value is not None:
                    return str(value)
        return None

    def _is_inflight(self, request):
        return bool(isinstance(request, dict) and request.get("in_flight"))

    def _queue_used(self):
        return len(self._completions) + len(self._inflight)

    def _ensure_open(self):
        if self._closed:
            raise IouringError("EBADF", "ring is closed")

    def _reject(self, code, opcode, task, index=None):
        self._rejected += 1
        event = {
            "task": task,
            "build_task": task,
            "task_id": task,
            "opcode": opcode,
            "error": code,
        }
        if index is not None:
            event["request_index"] = index
        self._audit.append(event)
        return {
            "ok": False,
            "error": code,
            "executed": False,
            "task": task,
            "opcode": opcode,
            "audit": copy.deepcopy(event),
        }
