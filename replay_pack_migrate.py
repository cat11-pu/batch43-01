import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


class ReplayError(ValueError):
    """Raised when a replay pack migration is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_MAGIC = "RPLY"
_UINT32_MASK = 0xFFFFFFFF
_MANIFEST_NAME = ".replay_pack_migrate_manifest.json"


class ReplayPackMigrate:
    """Replay pack inspection, atomic migration, verification and rollback."""

    def __init__(self, defaults=None, target_version=3, supported=(1, 2, 3)):
        self.defaults = dict(defaults or {})
        self.target_version = self._unsigned(target_version)
        self.supported = tuple(self._unsigned(v) for v in supported)
        self._migrated_hashes = {}

    @staticmethod
    def _unsigned(version):
        return int(version) & _UINT32_MASK

    @staticmethod
    def _checksum(chunks):
        digest = hashlib.sha256()
        for chunk in chunks:
            digest.update(chunk["data"].encode("utf-8"))
        return digest.hexdigest()

    def _load(self, path):
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise ReplayError("unreadable_pack", str(exc))
        try:
            pack = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayError("invalid_pack", str(exc))
        if not isinstance(pack, dict) or pack.get("magic") != _MAGIC:
            raise ReplayError("invalid_pack", "bad magic")
        pack["version"] = self._unsigned(pack.get("version", 0))
        return pack, raw

    @staticmethod
    def _manifest_path(directory):
        return Path(directory) / _MANIFEST_NAME

    def _known_migrations(self, directory):
        try:
            data = json.loads(
                self._manifest_path(directory).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _record_migration(self, directory, content_hash, dest):
        path = self._manifest_path(directory)
        try:
            known = self._known_migrations(directory)
            known[content_hash] = str(dest)
            fd, temp_name = tempfile.mkstemp(
                dir=str(directory), prefix=".manifest.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(known, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except OSError:
            pass

    @staticmethod
    def _acquire_lock(lock_path, timeout=2.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ReplayError("locked", str(lock_path))
                time.sleep(0.01)
            except OSError:
                return

    @staticmethod
    def _release_lock(lock_path):
        try:
            os.unlink(lock_path)
        except OSError:
            pass

    @staticmethod
    def _fsync_dir(directory):
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _write_atomic(dest, payload, crash_after=None):
        dest = Path(dest)
        temp_name = None
        try:
            try:
                fd, temp_name = tempfile.mkstemp(
                    dir=str(dest.parent), prefix=dest.name + ".",
                    suffix=".tmp")
            except OSError as exc:
                raise ReplayError("migration_failed", str(exc))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise ReplayError("migration_failed", str(exc))
            if crash_after == "temp_write":
                raise ReplayError("crashed", "simulated crash after temp write")
            try:
                os.replace(temp_name, dest)
            except OSError as exc:
                raise ReplayError("migration_failed", str(exc))
            temp_name = None
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def inspect(self, path):
        pack, _ = self._load(path)
        return {
            "magic": pack.get("magic"),
            "version": pack["version"],
            "duration_ms": pack.get("duration_ms"),
            "events": list(pack.get("events", [])),
            "chunks": list(pack.get("chunks", [])),
            "checksum": pack.get("checksum"),
        }

    def migrate(self, source, target=None, crash_after=None):
        source = Path(source)
        pack, raw = self._load(source)
        version = pack["version"]
        if version not in self.supported:
            raise ReplayError("unsupported_version", "version %d" % version)
        if version > self.target_version:
            raise ReplayError(
                "version_downgrade",
                "version %d above target %d" % (version, self.target_version))
        if version == self.target_version:
            return {"status": "already_migrated", "version": version,
                    "path": str(source)}

        content_hash = hashlib.sha256(raw).hexdigest()
        known = self._known_migrations(source.parent)
        if content_hash in self._migrated_hashes or content_hash in known:
            return {"status": "already_migrated",
                    "content_hash": content_hash}

        dest = Path(target) if target is not None else source
        in_place = os.path.abspath(dest) == os.path.abspath(source)

        new_pack = dict(pack)
        new_pack["version"] = self.target_version
        for key, value in self.defaults.items():
            new_pack.setdefault(key, value)
        payload = json.dumps(new_pack).encode("utf-8")

        lock_path = Path(str(dest) + ".migrate.lock")
        self._acquire_lock(lock_path)
        try:
            if in_place:
                backup = Path(str(source) + ".bak")
                try:
                    backup.write_bytes(raw)
                except OSError as exc:
                    raise ReplayError("migration_failed", str(exc))
            self._write_atomic(dest, payload, crash_after=crash_after)
            self._fsync_dir(dest.parent)
        finally:
            self._release_lock(lock_path)

        self._migrated_hashes[content_hash] = str(dest)
        self._record_migration(source.parent, content_hash, dest)
        return {"status": "migrated", "version": self.target_version,
                "content_hash": content_hash, "path": str(dest)}

    def verify(self, path):
        pack, _ = self._load(path)
        actual = self._checksum(pack.get("chunks", []))
        expected = pack.get("checksum")
        if expected != actual:
            return {"ok": False, "reason": "checksum_mismatch",
                    "expected": expected, "actual": actual}
        return {"ok": True, "version": pack["version"], "checksum": actual}

    def rollback(self, path):
        path = Path(path)
        backup = Path(str(path) + ".bak")
        try:
            data = backup.read_bytes()
        except OSError:
            raise ReplayError("no_backup", str(backup))
        self._write_atomic(path, data)
        self._fsync_dir(path.parent)
        return {"status": "rolled_back", "path": str(path)}
