import hashlib
import json

import pytest

import replay_pack_migrate as rpm


def checksum(chunks):
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk["data"].encode("utf-8"))
    return digest.hexdigest()


def write_pack(path, version=1, duration_ms=60000, events=None, chunks=None,
               **extra):
    events = events if events is not None else [
        {"t": 0, "type": "start"},
        {"t": 1500, "type": "input", "key": "W"},
        {"t": 4000, "type": "end"},
    ]
    chunks = chunks if chunks is not None else [
        {"id": 0, "data": "alpha"},
        {"id": 1, "data": "beta"},
    ]
    pack = {
        "magic": "RPLY",
        "version": version,
        "duration_ms": duration_ms,
        "events": events,
        "chunks": chunks,
        "checksum": checksum(chunks),
    }
    pack.update(extra)
    path.write_text(json.dumps(pack), encoding="utf-8")
    return pack


def make(**kwargs):
    return rpm.ReplayPackMigrate(**kwargs)


def test_inspect_reports_header_metadata(tmp_path):
    path = tmp_path / "replay.rpl"
    write_pack(path, version=2, duration_ms=12345)
    info = make().inspect(path)
    assert info["version"] == 2
    assert info["duration_ms"] == 12345
    assert len(info["events"]) == 3


def test_unsigned_wrap_version_is_not_treated_as_old(tmp_path):
    path = tmp_path / "wrapped.rpl"
    write_pack(path, version=4294967295)
    p = make()
    assert p.inspect(path)["version"] == 4294967295
    with pytest.raises(rpm.ReplayError) as exc:
        p.migrate(path)
    assert exc.value.code == "unsupported_version"


def test_missing_fields_use_explicit_default_table(tmp_path):
    path = tmp_path / "v1.rpl"
    write_pack(path, version=1)
    p = make(defaults={"camera": "default-cam", "fov": 90})
    p.migrate(path)
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["camera"] == "default-cam"
    assert migrated["fov"] == 90


def test_migration_preserves_duration(tmp_path):
    path = tmp_path / "v1.rpl"
    write_pack(path, version=1, duration_ms=98765)
    p = make()
    p.migrate(path)
    assert p.inspect(path)["duration_ms"] == 98765


def test_migration_preserves_event_order(tmp_path):
    path = tmp_path / "v1.rpl"
    events = [{"t": 30, "type": "c"}, {"t": 10, "type": "a"},
              {"t": 20, "type": "b"}]
    write_pack(path, version=1, events=events)
    p = make()
    p.migrate(path)
    assert p.inspect(path)["events"] == events


def test_in_place_migration_writes_atomically_and_keeps_a_backup(tmp_path):
    path = tmp_path / "v1.rpl"
    original = write_pack(path, version=1)
    p = make()
    out = p.migrate(path)
    assert out["status"] == "migrated"
    assert p.inspect(path)["version"] == 3
    assert json.loads((tmp_path / "v1.rpl.bak").read_text(encoding="utf-8"))["version"] == 1
    assert original["duration_ms"] == p.inspect(path)["duration_ms"]


def test_crash_during_temp_write_leaves_original_readable(tmp_path):
    path = tmp_path / "v1.rpl"
    before = write_pack(path, version=1)
    p = make()
    with pytest.raises(rpm.ReplayError) as exc:
        p.migrate(path, crash_after="temp_write")
    assert exc.value.code == "crashed"
    assert json.loads(path.read_text(encoding="utf-8")) == before
    assert p.inspect(path)["version"] == 1


def test_same_content_is_migrated_only_once(tmp_path):
    source = tmp_path / "shared.rpl"
    write_pack(source, version=1)
    p = make()
    first = p.migrate(source, target=tmp_path / "out-a.rpl")
    second = p.migrate(source, target=tmp_path / "out-b.rpl")
    assert first["status"] == "migrated"
    assert second["status"] == "already_migrated"
    assert not (tmp_path / "out-b.rpl").exists()


def test_pack_already_at_target_version_is_idempotent(tmp_path):
    path = tmp_path / "v3.rpl"
    write_pack(path, version=3)
    out = make().migrate(path)
    assert out["status"] == "already_migrated"


def test_verify_accepts_a_valid_checksum(tmp_path):
    path = tmp_path / "ok.rpl"
    write_pack(path, version=3)
    out = make().verify(path)
    assert out["ok"] is True


def test_verify_detects_corrupted_chunk_data(tmp_path):
    path = tmp_path / "bad.rpl"
    pack = write_pack(path, version=3)
    pack["chunks"][1]["data"] = "tampered"
    path.write_text(json.dumps(pack), encoding="utf-8")
    out = make().verify(path)
    assert out["ok"] is False
    assert out["reason"] == "checksum_mismatch"


def test_rollback_restores_the_pre_migration_file(tmp_path):
    path = tmp_path / "v1.rpl"
    write_pack(path, version=1)
    original_bytes = path.read_bytes()
    p = make()
    p.migrate(path)
    assert p.inspect(path)["version"] == 3
    out = p.rollback(path)
    assert out["status"] == "rolled_back"
    assert path.read_bytes() == original_bytes
    assert p.inspect(path)["version"] == 1


def test_version_downgrade_is_rejected(tmp_path):
    path = tmp_path / "v3.rpl"
    write_pack(path, version=3)
    p = make(target_version=2, supported=(1, 2, 3))
    with pytest.raises(rpm.ReplayError) as exc:
        p.migrate(path, target=tmp_path / "old.rpl")
    assert exc.value.code == "version_downgrade"


def test_unknown_future_version_is_rejected(tmp_path):
    path = tmp_path / "v4.rpl"
    write_pack(path, version=4)
    with pytest.raises(rpm.ReplayError) as exc:
        make().migrate(path)
    assert exc.value.code == "unsupported_version"


def test_failed_write_keeps_the_source_intact(tmp_path):
    path = tmp_path / "v1.rpl"
    write_pack(path, version=1)
    original = path.read_bytes()
    p = make()
    with pytest.raises(rpm.ReplayError) as exc:
        p.migrate(path, target=tmp_path / "missing" / "out.rpl")
    assert exc.value.code == "migration_failed"
    assert path.read_bytes() == original
    assert p.inspect(path)["version"] == 1
