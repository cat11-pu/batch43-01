import pytest

import pcr_sealing_session


def _sealer(**kwargs):
    return pcr_sealing_session.PcrSealingSession(expected_pcrs=(1, 2, 3), **kwargs)


def test_pcr_mismatch_fails_unseal():
    sealer = _sealer()
    handle = sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 9))
    assert exc.value.code == "TPM_PCR_MISMATCH"
    assert sealer.sealed_key("disk-1") is None


def test_pcr_mismatch_does_not_release_key_or_fallback():
    sealer = _sealer()
    handle = sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError):
        sealer.unseal(handle, (9, 9, 9))
    assert sealer.sealed_key("disk-1") is None
    assert sealer.counters()["unsealed_fallbacks"] == 0


def test_policy_session_can_only_be_used_once():
    sealer = _sealer()
    handle = sealer.start_policy()
    sealer.unseal(handle, (1, 2, 3))
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3))
    assert exc.value.code == "TPM_POLICY_REPLAY"


def test_reset_invalidates_outstanding_handle():
    sealer = _sealer()
    handle = sealer.start_policy()
    sealer.reset()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3))
    assert exc.value.code == "TPM_POLICY_INVALID"


def test_policy_slots_are_bounded():
    sealer = _sealer(max_slots=1)
    sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.start_policy()
    assert exc.value.code == "TPM_BUSY"


def test_unseal_without_policy_handle_requires_auth():
    sealer = _sealer()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(None, (1, 2, 3))
    assert exc.value.code == "TPM_AUTH_REQUIRED"


def test_boot_policy_change_invalidates_old_session():
    sealer = _sealer()
    handle = sealer.start_policy()
    sealer.boot_policy_change()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3))
    assert exc.value.code == "TPM_POLICY_INVALID"


def test_expired_signature_policy_rejected():
    sealer = _sealer()
    handle = sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3), signature_expired=True)
    assert exc.value.code == "TPM_POLICY_EXPIRED"


def test_pcr_extended_during_unseal_detected():
    sealer = _sealer()
    handle = sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3), pcr_extended=True)
    assert exc.value.code == "TPM_PCR_MISMATCH"


def test_same_policy_cannot_be_reused_for_another_disk():
    sealer = _sealer()
    handle = sealer.start_policy()
    sealer.unseal(handle, (1, 2, 3), key_id="disk-1")
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3), key_id="disk-2")
    assert exc.value.code == "TPM_POLICY_REPLAY"
    assert sealer.sealed_key("disk-2") is None


def test_wrong_hash_algorithm_does_not_match_pcrs():
    sealer = _sealer()
    handle = sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3), hash_alg="sha1")
    assert exc.value.code == "TPM_PCR_MISMATCH"


def test_successful_unseal_releases_key():
    sealer = _sealer()
    handle = sealer.start_policy()
    result = sealer.unseal(handle, (1, 2, 3))
    assert result["unsealed"] is True
    assert result["key"] == b"key-disk-1"
    assert sealer.sealed_key("disk-1") == b"key-disk-1"


def test_nonce_reuse_is_rejected():
    sealer = _sealer()
    sealer.start_policy(nonce="nonce-1")
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.start_policy(nonce="nonce-1")
    assert exc.value.code == "TPM_POLICY_REPLAY"


def test_module_reset_invalidates_old_handle_and_allows_rebuild():
    sealer = _sealer()
    handle = sealer.start_policy()
    sealer.module_reset()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3))
    assert exc.value.code == "TPM_POLICY_INVALID"
    rebuilt = sealer.start_policy()
    assert sealer.unseal(rebuilt, (1, 2, 3))["unsealed"] is True


def test_failed_unseal_does_not_consume_key_or_count_fallback():
    sealer = _sealer()
    handle = sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError):
        sealer.unseal(handle, (1, 2, 4), key_id="disk-9")
    assert sealer.sealed_key("disk-9") is None
    assert sealer.counters()["unsealed_fallbacks"] == 0
