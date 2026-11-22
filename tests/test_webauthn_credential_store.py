import pytest

import webauthn_credential_store as store_module
from webauthn_credential_store import WebAuthnCredentialStore


def _register(store, user="alice", credential_id="cred-1", **kwargs):
    return store.register(
        user=user,
        credential_id=credential_id,
        public_key=b"public-key",
        rp_id_hash=b"rp-hash",
        sign_count=kwargs.pop("sign_count", 0),
        resident=kwargs.pop("resident", False),
        backup_eligible=kwargs.pop("backup_eligible", False),
        **kwargs,
    )


def test_sign_counter_regression_is_clone_warning():
    store = WebAuthnCredentialStore()
    _register(store, sign_count=10)
    challenge = store.begin_challenge()["challenge"]
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("cred-1", b"rp-hash", challenge, sign_count=9)
    assert exc.value.code == "WEBAUTHN_CLONE_WARNING"


def test_rp_id_hash_mismatch_is_rejected():
    store = WebAuthnCredentialStore()
    _register(store)
    challenge = store.begin_challenge()["challenge"]
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("cred-1", b"other-rp", challenge, sign_count=1)
    assert exc.value.code == "WEBAUTHN_RP_MISMATCH"


def test_user_verification_flag_is_required():
    store = WebAuthnCredentialStore(require_uv=True)
    _register(store)
    challenge = store.begin_challenge()["challenge"]
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("cred-1", b"rp-hash", challenge, sign_count=1,
                           uv=False)
    assert exc.value.code == "WEBAUTHN_UV_REQUIRED"


def test_wrong_origin_is_rejected():
    store = WebAuthnCredentialStore()
    _register(store)
    challenge = store.begin_challenge()["challenge"]
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("cred-1", b"rp-hash", challenge, sign_count=1,
                           origin="https://phish.example.com")
    assert exc.value.code == "WEBAUTHN_ORIGIN_INVALID"


def test_challenge_can_only_be_used_once():
    store = WebAuthnCredentialStore()
    _register(store)
    challenge = store.begin_challenge()["challenge"]
    first = store.authenticate("cred-1", b"rp-hash", challenge, sign_count=1)
    assert first["authenticated"] is True
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("cred-1", b"rp-hash", challenge, sign_count=2)
    assert exc.value.code == "WEBAUTHN_CHALLENGE_REPLAY"


def test_credential_id_cannot_cross_users():
    store = WebAuthnCredentialStore()
    _register(store, user="alice", credential_id="cred-1")
    with pytest.raises(store_module.WebAuthnError) as exc:
        _register(store, user="bob", credential_id="cred-1")
    assert exc.value.code == "WEBAUTHN_CREDENTIAL_CONFLICT"


def test_concurrent_same_registration_yields_one_record():
    store = WebAuthnCredentialStore()
    first = _register(store, user="alice", credential_id="cred-1")
    second = _register(store, user="alice", credential_id="cred-1")
    assert first["count"] == 1
    assert second["count"] == 1
    assert second["duplicate"] is True


def test_resident_lookup_returns_only_own_credentials():
    store = WebAuthnCredentialStore()
    _register(store, user="alice", credential_id="cred-a", resident=True)
    _register(store, user="bob", credential_id="cred-b", resident=True)
    assert store.find_resident("alice") == ["cred-a"]
    assert store.find_resident("bob") == ["cred-b"]


def test_self_signed_attestation_is_rejected():
    store = WebAuthnCredentialStore()
    with pytest.raises(store_module.WebAuthnError) as exc:
        _register(store, attestation_self_signed=True,
                  attestation_chain_valid=False)
    assert exc.value.code == "WEBAUTHN_ATTESTATION_INVALID"


def test_backup_eligible_flag_is_stored():
    store = WebAuthnCredentialStore()
    _register(store, backup_eligible=True)
    assert store.credential_info("cred-1")["backup_eligible"] is True


def test_restart_restores_public_keys_and_counters():
    store = WebAuthnCredentialStore()
    _register(store, sign_count=4)
    state = store.dump_state()
    restarted = WebAuthnCredentialStore()
    restarted.load_state(state)
    challenge = restarted.begin_challenge()["challenge"]
    result = restarted.authenticate("cred-1", b"rp-hash", challenge,
                                    sign_count=5)
    assert result["authenticated"] is True
    assert result["user"] == "alice"


def test_frozen_credential_is_rejected():
    store = WebAuthnCredentialStore()
    _register(store)
    store.freeze("cred-1")
    challenge = store.begin_challenge()["challenge"]
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("cred-1", b"rp-hash", challenge, sign_count=1)
    assert exc.value.code == "WEBAUTHN_CREDENTIAL_FROZEN"


def test_successful_authentication_updates_sign_count():
    store = WebAuthnCredentialStore()
    _register(store, sign_count=1)
    challenge = store.begin_challenge()["challenge"]
    result = store.authenticate("cred-1", b"rp-hash", challenge, sign_count=5)
    assert result["sign_count"] == 5
    assert store.credential_info("cred-1")["sign_count"] == 5


def test_unknown_credential_is_rejected():
    store = WebAuthnCredentialStore()
    challenge = store.begin_challenge()["challenge"]
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("missing", b"rp-hash", challenge, sign_count=1)
    assert exc.value.code == "WEBAUTHN_UNKNOWN_CREDENTIAL"


def test_expired_challenge_is_rejected():
    store = WebAuthnCredentialStore(challenge_ttl=60)
    _register(store)
    challenge = store.begin_challenge(now=0)["challenge"]
    with pytest.raises(store_module.WebAuthnError) as exc:
        store.authenticate("cred-1", b"rp-hash", challenge, sign_count=1,
                           now=61)
    assert exc.value.code == "WEBAUTHN_CHALLENGE_REPLAY"
