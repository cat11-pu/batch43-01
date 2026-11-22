import pytest

import fscrypt_keyring_supervisor


def _supervisor(**kwargs):
    return fscrypt_keyring_supervisor.FscryptKeyringSupervisor(**kwargs)


def test_remove_with_open_handle_defers_eviction_and_keeps_readable():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"photo")
    supervisor.open_file("inode-1", "desc-1")
    supervisor.remove_key("desc-1")
    assert supervisor.key_status("desc-1") == "pending_evict"
    assert supervisor.read_file("inode-1") == b"photo"


def test_new_open_after_remove_request_returns_enokey():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"photo")
    supervisor.open_file("inode-1", "desc-1")
    supervisor.remove_key("desc-1")
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError) as exc:
        supervisor.open_file("inode-2", "desc-1")
    assert exc.value.code == "ENOKEY"


def test_close_last_handle_evicts_key_and_blocks_read():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"photo")
    supervisor.open_file("inode-1", "desc-1")
    supervisor.remove_key("desc-1")
    supervisor.close_file("inode-1")
    assert supervisor.key_status("desc-1") == "missing"
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError) as exc:
        supervisor.read_file("inode-1")
    assert exc.value.code == "ENOKEY"


def test_incompatible_policy_version_rejected():
    supervisor = _supervisor(policy_version=2)
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError) as exc:
        supervisor.add_key("desc-1", b"key-1", policy_version=1)
    assert exc.value.code == "FSCRYPT_POLICY_INCOMPAT"


def test_rotation_makes_new_writes_use_new_generation():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"old")
    supervisor.rotate_key("desc-1", b"key-2")
    supervisor.write_file("inode-2", "desc-1", b"new")
    assert supervisor.inode_generation("inode-1") == 1
    assert supervisor.inode_generation("inode-2") == 2


def test_old_file_remains_readable_after_rotation():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"old-data")
    supervisor.rotate_key("desc-1", b"key-2")
    assert supervisor.read_file("inode-1") == b"old-data"


def test_unsupported_filesystem_fails_closed():
    supervisor = _supervisor(fs_supports_encryption=False)
    supervisor.add_key("desc-1", b"key-1")
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError) as exc:
        supervisor.write_file("inode-1", "desc-1", b"plaintext")
    assert exc.value.code == "FSCRYPT_FS_UNSUPPORTED"


def test_evicted_key_returns_enokey_on_read():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"data")
    supervisor.remove_key("desc-1")
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError) as exc:
        supervisor.read_file("inode-1")
    assert exc.value.code == "ENOKEY"


def test_process_restart_keeps_session_keys():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"data")
    supervisor.restart()
    assert supervisor.key_status("desc-1") == "active"
    assert supervisor.read_file("inode-1") == b"data"


def test_session_keyring_reset_loses_keys():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"data")
    supervisor.restart(session_reset=True)
    assert supervisor.key_status("desc-1") == "missing"
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError) as exc:
        supervisor.read_file("inode-1")
    assert exc.value.code == "ENOKEY"


def test_duplicate_descriptor_is_rejected():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError) as exc:
        supervisor.add_key("desc-1", b"key-2")
    assert exc.value.code == "FSCRYPT_KEY_DESC_COLLISION"


def test_suspend_clears_keyring():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"data")
    supervisor.suspend()
    assert supervisor.key_status("desc-1") == "missing"
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError):
        supervisor.read_file("inode-1")


def test_remove_without_open_handles_evicts_immediately():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.write_file("inode-1", "desc-1", b"data")
    supervisor.remove_key("desc-1")
    assert supervisor.key_status("desc-1") == "missing"
    assert supervisor.files() == ["inode-1"]


def test_double_remove_is_idempotent():
    supervisor = _supervisor()
    supervisor.add_key("desc-1", b"key-1")
    supervisor.remove_key("desc-1")
    supervisor.remove_key("desc-1")
    assert supervisor.key_status("desc-1") == "missing"


def test_unsupported_write_creates_no_file():
    supervisor = _supervisor(fs_supports_encryption=False)
    supervisor.add_key("desc-1", b"key-1")
    with pytest.raises(fscrypt_keyring_supervisor.FscryptError):
        supervisor.write_file("inode-1", "desc-1", b"plaintext")
    assert supervisor.files() == []
