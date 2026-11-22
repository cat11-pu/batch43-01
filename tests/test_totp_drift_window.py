import base64
import hashlib
import hmac
import struct

import pytest

import totp_drift_window

SECRET_A = b"12345678901234567890"
SECRET_B = b"09876543210987654321"


def _b32(secret):
    return base64.b32encode(secret).decode()


def _code(secret, counter, algorithm="SHA1", digits=6):
    digest = hmac.new(secret, struct.pack(">Q", counter),
                      getattr(hashlib, algorithm.lower())).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _store(**kwargs):
    store = totp_drift_window.TotpDriftWindow(**kwargs)
    store.enroll("alice", _b32(SECRET_A))
    return store


def test_code_can_only_be_used_once():
    store = _store()
    code = _code(SECRET_A, 100)
    assert store.verify("alice", code, timestamp=3000)["ok"] is True
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.verify("alice", code, timestamp=3000)
    assert exc.value.code == "TOTP_REPLAY"


def test_code_one_step_ahead_is_accepted():
    store = _store()
    result = store.verify("alice", _code(SECRET_A, 101), timestamp=3000)
    assert result["ok"] is True
    assert result["counter"] == 101


def test_code_two_steps_ahead_is_rejected():
    store = _store()
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.verify("alice", _code(SECRET_A, 102), timestamp=3000)
    assert exc.value.code == "TOTP_WINDOW"


def test_code_one_step_behind_is_accepted():
    store = _store()
    result = store.verify("alice", _code(SECRET_A, 99), timestamp=3000)
    assert result["ok"] is True
    assert result["counter"] == 99


def test_sha256_user_accepts_sha256_code():
    store = totp_drift_window.TotpDriftWindow()
    store.enroll("alice", _b32(SECRET_A), algorithm="SHA256")
    code = _code(SECRET_A, 100, algorithm="SHA256")
    assert store.verify("alice", code, timestamp=3000,
                        algorithm="SHA256")["ok"] is True


def test_sha1_code_does_not_satisfy_sha256_user():
    store = totp_drift_window.TotpDriftWindow()
    store.enroll("alice", _b32(SECRET_A), algorithm="SHA256")
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.verify("alice", _code(SECRET_A, 100), timestamp=3000,
                     algorithm="SHA1")
    assert exc.value.code == "TOTP_ALGORITHM_MISMATCH"


def test_backup_code_can_only_be_used_once():
    store = _store()
    code = store.generate_backup_codes("alice")["codes"][0]
    assert store.use_backup_code("alice", code)["used"] is True
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.use_backup_code("alice", code)
    assert exc.value.code == "TOTP_REPLAY"


def test_clock_rollback_does_not_reaccept_old_code():
    store = _store()
    code = _code(SECRET_A, 100)
    store.verify("alice", code, timestamp=3000)
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.verify("alice", code, timestamp=2970)
    assert exc.value.code == "TOTP_REPLAY"


def test_restart_restores_last_counter():
    store = _store()
    code = _code(SECRET_A, 100)
    store.verify("alice", code, timestamp=3000)
    state = store.dump_state()
    restarted = totp_drift_window.TotpDriftWindow()
    restarted.load_state(state)
    with pytest.raises(totp_drift_window.TotpError) as exc:
        restarted.verify("alice", code, timestamp=3000)
    assert exc.value.code == "TOTP_REPLAY"


def test_secret_cannot_be_shared_between_users():
    store = _store()
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.enroll("bob", _b32(SECRET_A))
    assert exc.value.code == "TOTP_SECRET_REUSE"


def test_provisioning_uri_does_not_leak_secret_to_log():
    store = _store()
    uri = store.provisioning_uri("alice")
    assert _b32(SECRET_A) in uri
    joined = "".join(store.access_log())
    assert _b32(SECRET_A) not in joined
    assert SECRET_A.decode() not in joined


def test_account_locks_after_repeated_failures():
    store = _store(max_failures=5)
    for _ in range(5):
        with pytest.raises(totp_drift_window.TotpError):
            store.verify("alice", "bad-code", timestamp=3000,
                         source_ip="203.0.113.%d" % _)
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.verify("alice", _code(SECRET_A, 100), timestamp=3000)
    assert exc.value.code == "TOTP_LOCKED"


def test_users_have_independent_secrets():
    store = _store()
    store.enroll("bob", _b32(SECRET_B))
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.verify("bob", _code(SECRET_A, 100), timestamp=3000)
    assert exc.value.code == "TOTP_WINDOW"


def test_per_user_drift_is_applied_separately():
    store = _store()
    store.enroll("bob", _b32(SECRET_B))
    store.set_drift("alice", 60)
    alice_code = _code(SECRET_A, 102)
    bob_code = _code(SECRET_B, 102)
    assert store.verify("alice", alice_code, timestamp=3000)["ok"] is True
    with pytest.raises(totp_drift_window.TotpError) as exc:
        store.verify("bob", bob_code, timestamp=3000)
    assert exc.value.code == "TOTP_WINDOW"


def test_valid_code_updates_last_counter():
    store = _store()
    result = store.verify("alice", _code(SECRET_A, 100), timestamp=3000)
    assert result["ok"] is True
    assert result["counter"] == 100
    assert result["last_counter"] == 100
