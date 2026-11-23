import concurrent.futures

import pytest

import pcr_sealing_session


def _sealer(**kwargs):
    return pcr_sealing_session.PcrSealingSession(expected_pcrs=(1, 2, 3), **kwargs)


def test_policy_session_and_released_key_survive_service_restart(tmp_path):
    state_path = tmp_path / "tpm-state.json"
    first = _sealer(state_path=state_path)
    handle = first.start_policy(nonce="restart-nonce")

    second = _sealer(state_path=state_path)
    assert second.policy_count() == 1
    assert second.unseal(handle, (1, 2, 3))["unsealed"] is True

    third = _sealer(state_path=state_path)
    assert third.sealed_key("disk-1") == b"key-disk-1"
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        third.unseal(handle, (1, 2, 3))
    assert exc.value.code == "TPM_POLICY_REPLAY"


def test_module_reset_is_persisted_and_new_policy_can_be_built(tmp_path):
    state_path = tmp_path / "tpm-state.json"
    first = _sealer(state_path=state_path)
    handle = first.start_policy()
    first.module_reset()

    restarted = _sealer(state_path=state_path)
    assert restarted.policy_count() == 0
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        restarted.unseal(handle, (1, 2, 3))
    assert exc.value.code == "TPM_POLICY_INVALID"

    rebuilt = restarted.start_policy()
    assert restarted.unseal(rebuilt, (1, 2, 3))["unsealed"] is True


def test_unseal_audit_records_pcr_group_and_policy_session():
    sealer = _sealer()
    handle = sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError):
        sealer.unseal(handle, (1, 2, 9), hash_alg="sha256")

    record = sealer.audit_log()[0]
    assert record["policy_handle"] == handle
    assert record["policy_session"] == handle
    assert record["pcr_group"] == [1, 2, 3]
    assert record["hash_alg"] == "sha256"
    assert record["code"] == "TPM_PCR_MISMATCH"


def test_signature_policy_deadline_is_enforced():
    sealer = _sealer()
    handle = sealer.start_policy(expires_at=50)
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3), now=50)
    assert exc.value.code == "TPM_POLICY_EXPIRED"
    assert sealer.sealed_key("disk-1") is None


def test_hash_algorithm_selects_the_matching_pcr_group():
    sealer = pcr_sealing_session.PcrSealingSession(
        expected_pcrs={"sha256": (1, 2, 3), "sha1": (7, 8, 9)}
    )
    sha1_handle = sealer.start_policy(hash_alg="sha1")
    assert sealer.unseal(sha1_handle, (7, 8, 9), hash_alg="sha1")["unsealed"] is True

    sha256_handle = sealer.start_policy(hash_alg="sha256")
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(sha256_handle, (7, 8, 9), hash_alg="sha1")
    assert exc.value.code == "TPM_PCR_MISMATCH"


def test_authentication_failures_trigger_lockout():
    sealer = _sealer()
    for _ in range(3):
        with pytest.raises(pcr_sealing_session.TpmError) as exc:
            sealer.unseal(None, (1, 2, 3))
        assert exc.value.code == "TPM_AUTH_REQUIRED"

    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(None, (1, 2, 3))
    assert exc.value.code == "TPM_LOCKED"
    assert sealer.counters()["locked_out"] is True


def test_hardware_communication_failure_is_distinct_from_policy_mismatch(monkeypatch):
    sealer = _sealer()
    handle = sealer.start_policy()

    def fail_read(*args, **kwargs):
        raise OSError("transport unavailable")

    monkeypatch.setattr(sealer, "_read_pcrs", fail_read)
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.unseal(handle, (1, 2, 3))
    assert exc.value.code == "TPM_COMM_FAILURE"
    assert exc.value.kind == "hardware"
    assert sealer.sealed_key("disk-1") is None
    assert sealer.counters()["pcr_mismatches"] == 0


def test_concurrent_unseals_are_serialized_across_policy_slots():
    sealer = _sealer(max_slots=2)
    handles = [sealer.start_policy(), sealer.start_policy()]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(sealer.unseal, handle, (1, 2, 3), key_id=f"disk-{index}")
            for index, handle in enumerate(handles)
        ]
        results = [future.result() for future in futures]

    assert all(result["unsealed"] is True for result in results)
    assert sealer.policy_count() == 0
    assert sealer.counters()["unseal_successes"] == 2


def test_generated_nonce_collision_is_rejected(monkeypatch):
    monkeypatch.setattr(pcr_sealing_session.secrets, "token_hex", lambda size: "fixed")
    sealer = _sealer(max_slots=2)
    sealer.start_policy()
    with pytest.raises(pcr_sealing_session.TpmError) as exc:
        sealer.start_policy()
    assert exc.value.code == "TPM_POLICY_REPLAY"


def test_empty_state_file_is_treated_as_clean_state(tmp_path):
    state_path = tmp_path / "tpm-state.json"
    state_path.touch()
    sealer = _sealer(state_path=state_path)
    handle = sealer.start_policy()
    assert sealer.unseal(handle, (1, 2, 3))["unsealed"] is True
