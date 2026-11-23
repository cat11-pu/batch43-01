"""fscrypt keyring and inode encryption supervisor."""

from __future__ import annotations

from dataclasses import dataclass, field
import hmac
import os
import threading


class FscryptError(Exception):
    """Key or policy failure raised by the supervisor."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


@dataclass
class _KeyRecord:
    descriptor: object
    policy_version: int
    current_generation: int
    generation_keys: dict[int, bytes] = field(default_factory=dict)
    status: str = "active"


@dataclass
class _FileRecord:
    descriptor: object
    generation: int
    policy_version: int
    key_id: bytes
    payload: bytes


class FscryptKeyringSupervisor:
    """Tracks encryption keys, open handles and per-inode key generations."""

    _MAGIC = b"FSCR"
    _NONCE_SIZE = 16
    _TAG_SIZE = 32

    def __init__(self, fs_supports_encryption=True, policy_version=2):
        self.fs_supports_encryption = fs_supports_encryption
        self.policy_version = policy_version

        self._keys: dict[object, _KeyRecord] = {}
        self._files: dict[object, _FileRecord] = {}
        self._open_handles: dict[object, int] = {}
        self._lock = threading.RLock()

    def add_key(self, descriptor, master_key, policy_version=None):
        """Install a master key for a descriptor and policy version."""
        with self._lock:
            version = self.policy_version if policy_version is None else policy_version
            if version != self.policy_version:
                raise FscryptError(
                    "FSCRYPT_POLICY_INCOMPAT",
                    "key policy version is incompatible with the filesystem policy",
                )
            if descriptor in self._keys:
                raise FscryptError(
                    "FSCRYPT_KEY_DESC_COLLISION",
                    "a key with this descriptor is already present",
                )

            master = self._as_bytes(master_key, "master key")
            self._keys[descriptor] = _KeyRecord(
                descriptor=descriptor,
                policy_version=version,
                current_generation=1,
                generation_keys={1: master},
            )

    def open_file(self, inode_id, descriptor):
        """Open an existing inode, taking a reference on its key generation."""
        with self._lock:
            key = self._keys.get(descriptor)
            if key is None or key.status != "active":
                raise FscryptError("ENOKEY", "encryption key is not available")

            record = self._files.get(inode_id)
            if record is None:
                raise FscryptError("ENOENT", "encrypted inode does not exist")
            if record.descriptor != descriptor:
                raise FscryptError("ENOKEY", "inode uses a different key descriptor")

            master = key.generation_keys.get(record.generation)
            if master is None:
                raise FscryptError("ENOKEY", "key generation is no longer available")
            if not hmac.compare_digest(record.key_id, self._key_id(master, record)):
                raise FscryptError("ENOKEY", "encryption key does not match inode")

            self._open_handles[inode_id] = self._open_handles.get(inode_id, 0) + 1
            return inode_id

    def close_file(self, inode_id):
        """Drop one open handle and evict a pending key after the last one."""
        with self._lock:
            count = self._open_handles.get(inode_id, 0)
            if count == 0:
                return
            if count > 1:
                self._open_handles[inode_id] = count - 1
                return

            del self._open_handles[inode_id]
            record = self._files.get(inode_id)
            if record is not None:
                self._evict_if_unused(record.descriptor)

    def remove_key(self, descriptor):
        """Request eviction, deferring it while inode handles still exist."""
        with self._lock:
            key = self._keys.get(descriptor)
            if key is None:
                return
            if self._descriptor_open_count(descriptor):
                key.status = "pending_evict"
            else:
                self._evict_key(descriptor)

    def write_file(self, inode_id, descriptor, data):
        """Encrypt and store data, binding new inodes to the current generation."""
        with self._lock:
            if not self.fs_supports_encryption:
                raise FscryptError(
                    "FSCRYPT_FS_UNSUPPORTED",
                    "filesystem does not support encryption; refusing plaintext write",
                )

            plaintext = self._as_bytes(data, "file data")
            record = self._files.get(inode_id)

            if record is not None:
                if record.descriptor != descriptor:
                    raise FscryptError("ENOKEY", "inode uses a different key descriptor")
                key = self._keys.get(descriptor)
                if key is None:
                    raise FscryptError("ENOKEY", "encryption key is not available")
                if key.status == "pending_evict" and inode_id not in self._open_handles:
                    raise FscryptError("ENOKEY", "encryption key is pending eviction")
                master = key.generation_keys.get(record.generation)
                if master is None:
                    raise FscryptError("ENOKEY", "key generation is no longer available")
                if not hmac.compare_digest(record.key_id, self._key_id(master, record)):
                    raise FscryptError("ENOKEY", "encryption key does not match inode")

                payload = self._encrypt(record, master, plaintext)
                self._files[inode_id] = _FileRecord(
                    descriptor=record.descriptor,
                    generation=record.generation,
                    policy_version=record.policy_version,
                    key_id=record.key_id,
                    payload=payload,
                )
                return

            key = self._keys.get(descriptor)
            if key is None or key.status != "active":
                raise FscryptError("ENOKEY", "encryption key is not available")
            generation = key.current_generation
            master = key.generation_keys[generation]
            new_record = _FileRecord(
                descriptor=descriptor,
                generation=generation,
                policy_version=key.policy_version,
                key_id=b"",
                payload=b"",
            )
            new_record.key_id = self._key_id(master, new_record)
            new_record.payload = self._encrypt(new_record, master, plaintext)
            self._files[inode_id] = new_record

    def read_file(self, inode_id):
        """Decrypt an inode, distinguishing a missing key from damaged data."""
        with self._lock:
            record = self._files.get(inode_id)
            if record is None:
                raise FscryptError("ENOENT", "encrypted inode does not exist")

            key = self._keys.get(record.descriptor)
            if key is None:
                raise FscryptError("ENOKEY", "encryption key is not available")
            if key.status == "pending_evict" and inode_id not in self._open_handles:
                raise FscryptError("ENOKEY", "encryption key is pending eviction")

            master = key.generation_keys.get(record.generation)
            if master is None:
                raise FscryptError("ENOKEY", "key generation is no longer available")
            if not hmac.compare_digest(record.key_id, self._key_id(master, record)):
                raise FscryptError("ENOKEY", "encryption key does not match inode")
            return self._decrypt(record, master)

    def rotate_key(self, descriptor, new_master_key):
        """Start a new key generation while retaining prior generations."""
        with self._lock:
            key = self._keys.get(descriptor)
            if key is None or key.status != "active":
                raise FscryptError("ENOKEY", "encryption key is not available")

            new_master = self._as_bytes(new_master_key, "master key")
            generation = key.current_generation + 1
            key.generation_keys[generation] = new_master
            key.current_generation = generation

    def restart(self, session_reset=False):
        """Simulate a process restart; persistent session keys survive by default."""
        with self._lock:
            self._open_handles.clear()
            if session_reset:
                for key in self._keys.values():
                    key.generation_keys.clear()
                self._keys.clear()
                return

            for descriptor, key in list(self._keys.items()):
                if key.status == "pending_evict":
                    self._evict_key(descriptor)

    def suspend(self):
        """Clear volatile keyring state when the device enters sleep."""
        with self._lock:
            self._open_handles.clear()
            for key in self._keys.values():
                key.generation_keys.clear()
            self._keys.clear()

    def key_status(self, descriptor):
        with self._lock:
            key = self._keys.get(descriptor)
            if key is None:
                return "missing"
            return key.status

    def inode_generation(self, inode_id):
        with self._lock:
            record = self._files.get(inode_id)
            return record.generation if record is not None else 0

    def files(self):
        with self._lock:
            return list(self._files)

    def _descriptor_open_count(self, descriptor):
        count = 0
        for inode_id in self._open_handles:
            record = self._files.get(inode_id)
            if record is not None and record.descriptor == descriptor:
                count += 1
        return count

    def _evict_if_unused(self, descriptor):
        key = self._keys.get(descriptor)
        if key is not None and key.status == "pending_evict":
            if not self._descriptor_open_count(descriptor):
                self._evict_key(descriptor)

    def _evict_key(self, descriptor):
        key = self._keys.pop(descriptor, None)
        if key is not None:
            key.generation_keys.clear()

    @staticmethod
    def _as_bytes(value, label):
        if isinstance(value, bytes):
            return value
        if isinstance(value, (bytearray, memoryview)):
            return bytes(value)
        raise FscryptError("EINVAL", f"{label} must be bytes-like")

    @staticmethod
    def _descriptor_bytes(descriptor):
        if isinstance(descriptor, bytes):
            return b"b:" + descriptor
        if isinstance(descriptor, str):
            return b"s:" + descriptor.encode("utf-8", "surrogatepass")
        return b"r:" + repr(descriptor).encode("utf-8", "backslashreplace")

    def _derive(self, master, record, purpose):
        descriptor = self._descriptor_bytes(record.descriptor)
        message = b"".join(
            (
                b"fscrypt-supervisor\x00",
                purpose,
                b"\x00",
                int(record.policy_version).to_bytes(4, "big"),
                int(record.generation).to_bytes(8, "big"),
                len(descriptor).to_bytes(4, "big"),
                descriptor,
            )
        )
        return hmac.digest(master, message, "sha256")

    def _key_id(self, master, record):
        return self._derive(master, record, b"key-id")

    def _encrypt(self, record, master, plaintext):
        enc_key = self._derive(master, record, b"contents")
        mac_key = self._derive(master, record, b"integrity")
        nonce = os.urandom(self._NONCE_SIZE)
        ciphertext = self._xor_stream(enc_key, nonce, plaintext)
        tag = hmac.digest(mac_key, nonce + ciphertext, "sha256")
        return self._MAGIC + nonce + ciphertext + tag

    def _decrypt(self, record, master):
        payload = record.payload
        minimum = len(self._MAGIC) + self._NONCE_SIZE + self._TAG_SIZE
        if len(payload) < minimum or not payload.startswith(self._MAGIC):
            raise FscryptError("EIO", "encrypted file is damaged")

        nonce_start = len(self._MAGIC)
        cipher_start = nonce_start + self._NONCE_SIZE
        tag_start = len(payload) - self._TAG_SIZE
        nonce = payload[nonce_start:cipher_start]
        ciphertext = payload[cipher_start:tag_start]
        tag = payload[tag_start:]

        mac_key = self._derive(master, record, b"integrity")
        expected = hmac.digest(mac_key, nonce + ciphertext, "sha256")
        if not hmac.compare_digest(tag, expected):
            raise FscryptError("EIO", "encrypted file is damaged or has been tampered with")

        enc_key = self._derive(master, record, b"contents")
        return self._xor_stream(enc_key, nonce, ciphertext)

    @staticmethod
    def _xor_stream(key, nonce, data):
        result = bytearray(len(data))
        offset = 0
        counter = 0
        while offset < len(data):
            block = hmac.digest(
                key,
                b"stream\x00" + nonce + counter.to_bytes(8, "big"),
                "sha256",
            )
            take = min(len(block), len(data) - offset)
            for index in range(take):
                result[offset + index] = data[offset + index] ^ block[index]
            offset += take
            counter += 1
        return bytes(result)
