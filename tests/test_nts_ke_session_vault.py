import pytest

import nts_ke_session_vault


SERVER = "time.corp.example"


def _vault(**kwargs):
    return nts_ke_session_vault.NtsKeSessionVault(**kwargs)


def _est(vault, **kwargs):
    return vault.establish(
        SERVER,
        kwargs.pop("expected_host", SERVER),
        kwargs.pop("cert_cn", SERVER),
        **kwargs,
    )


def test_certificate_name_mismatch_is_rejected():
    vault = _vault()
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        _est(vault, cert_cn="impostor.example")
    assert exc.value.code == "NTS_KE_TLS_INVALID"
    assert vault.credentials(SERVER) is None


def test_expected_hostname_mismatch_is_rejected():
    vault = _vault()
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        _est(vault, expected_host="other.example")
    assert exc.value.code == "NTS_KE_TLS_INVALID"


def test_after_tls_failure_poll_stops_without_plain_ntp():
    vault = _vault()
    with pytest.raises(nts_ke_session_vault.NtsError):
        _est(vault, cert_cn="impostor.example")
    result = vault.poll(SERVER)
    assert result["action"] == "stop"
    assert vault.counters()["plain_ntp_requests"] == 0


def test_nonce_replay_is_rejected_and_clock_untouched():
    vault = _vault()
    _est(vault)
    first = vault.sync(SERVER, "nonce-1", "unique-1", now=0.0, server_time=100.0)
    assert first["ok"] is True
    before = vault.clock()
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        vault.sync(SERVER, "nonce-1", "unique-2", now=1.0, server_time=50.0)
    assert exc.value.code == "NTS_NONCE_REPLAY"
    assert vault.clock() == before


def test_unique_identifier_mismatch_rejected():
    vault = _vault()
    _est(vault)
    vault.sync(SERVER, "nonce-1", "unique-1", now=0.0)
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        vault.sync(SERVER, "nonce-2", "unique-other", now=1.0)
    assert exc.value.code == "NTS_UNIQUE_ID_MISMATCH"


def test_algorithm_outside_allowlist_aborts_handshake():
    vault = _vault(allowed_algorithms=("AEAD_AES_SIV_CMAC_256",))
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        _est(vault, algorithm="AEAD_AES_128_GCM")
    assert exc.value.code == "NTS_AEAD_UNSUPPORTED"
    assert vault.credentials(SERVER) is None


def test_credentials_survive_process_restart(tmp_path):
    state = str(tmp_path / "nts.json")
    vault = _vault(state_path=state)
    _est(vault)
    restarted = _vault(state_path=state)
    credential = restarted.credentials(SERVER)
    assert credential is not None
    assert restarted.sync(SERVER, "nonce-2", "unique-2", now=1.0)["ok"] is True


def test_empty_vault_stops_polling_without_downgrade():
    vault = _vault()
    result = vault.poll(SERVER)
    assert result["action"] == "stop"
    assert result["code"] == "NO_CREDENTIAL"
    assert vault.counters()["plain_ntp_requests"] == 0


def test_expired_credential_cannot_be_used():
    vault = _vault()
    _est(vault, lifetime=10.0, now=0.0)
    assert vault.credentials(SERVER, now=20.0) is None
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        vault.sync(SERVER, "nonce-1", "unique-1", now=20.0)
    assert exc.value.code == "NTS_CREDENTIAL_EXPIRED"
    assert vault.poll(SERVER, now=20.0)["action"] == "stop"


def test_credential_is_bound_to_its_server():
    vault = _vault()
    _est(vault)
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        vault.sync("time.other.example", "nonce-1", "unique-1", now=0.0)
    assert exc.value.code == "NTS_KE_TLS_INVALID"
    assert vault.credentials("time.other.example") is None


def test_master_key_rotation_invalidates_credentials():
    vault = _vault()
    _est(vault)
    vault.rotate_master_key(SERVER)
    with pytest.raises(nts_ke_session_vault.NtsError) as exc:
        vault.sync(SERVER, "nonce-1", "unique-1", now=1.0)
    assert exc.value.code == "NTS_CREDENTIAL_EXPIRED"
    _est(vault, now=2.0)
    assert vault.sync(SERVER, "nonce-2", "unique-2", now=2.0)["ok"] is True


def test_successful_sync_adjusts_clock():
    vault = _vault()
    _est(vault)
    result = vault.sync(SERVER, "nonce-1", "unique-1", now=100.0, server_time=112.5)
    assert result["ok"] is True
    assert result["offset"] == 12.5
    assert vault.clock() == 12.5


def test_established_credential_exposes_algorithm_and_server():
    vault = _vault()
    credential = _est(vault)
    assert credential["server"] == SERVER
    assert credential["algorithm"] == "AEAD_AES_SIV_CMAC_256"
    assert credential["expires_at"] > 0


def test_failed_handshake_can_be_retried_successfully():
    vault = _vault()
    with pytest.raises(nts_ke_session_vault.NtsError):
        _est(vault, cert_cn="impostor.example")
    credential = _est(vault, now=1.0)
    assert credential["server"] == SERVER
    assert vault.poll(SERVER, now=1.0)["action"] == "sync"


def test_valid_credential_poll_requests_authenticated_sync():
    vault = _vault()
    _est(vault)
    result = vault.poll(SERVER)
    assert result["action"] == "sync"
    assert result["server"] == SERVER
