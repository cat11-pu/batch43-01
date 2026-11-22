import pytest

import ima_appraisal_log


def test_appraisal_mismatch_denies_execution():
    ima = ima_appraisal_log.ImaAppraisalLog()
    with pytest.raises(ima_appraisal_log.ImaError) as exc:
        ima.appraise("app-1", trusted_hash="aaaa", measured_hash="bbbb")
    assert exc.value.code == "IMA_APPRAISAL_FAILED"


def test_appraisal_match_allows_execution():
    ima = ima_appraisal_log.ImaAppraisalLog()
    assert ima.appraise("app-1", trusted_hash="aaaa",
                        measured_hash="aaaa")["allowed"] is True


def test_open_write_execute_is_denied():
    ima = ima_appraisal_log.ImaAppraisalLog()
    ima.open_for_write("app-1")
    with pytest.raises(ima_appraisal_log.ImaError) as exc:
        ima.execute("app-1")
    assert exc.value.code == "IMA_TOMTOU_DENIED"


def test_create_write_execute_delete_are_all_measured():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    for event in ("create", "write", "execute", "delete"):
        ima.measure("app-1", b"content", event)
    events = [entry["event"] for entry in ima.log_entries()]
    assert events == ["create", "write", "execute", "delete"]


def test_mmap_loaded_binary_is_measured():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    ima.measure("lib-1", b"code", "mmap")
    assert ima.log_entries()[0]["event"] == "mmap"


def test_restart_keeps_old_log_entries():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    ima.measure("app-1", b"content", "create")
    state = ima.dump_state()
    restarted = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    restarted.load_state(state)
    assert [e["event"] for e in restarted.log_entries()] == ["create"]


def test_sequence_continues_without_overwriting_old_records():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    ima.measure("app-1", b"content", "create")
    state = ima.dump_state()
    restarted = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    restarted.load_state(state)
    restarted.measure("app-1", b"content", "execute")
    sequences = [entry["sequence"] for entry in restarted.log_entries()]
    assert sequences == [0, 1]


def test_full_log_raises_instead_of_overwriting():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=2)
    ima.measure("app-1", b"a", "create")
    ima.measure("app-1", b"b", "write")
    with pytest.raises(ima_appraisal_log.ImaError) as exc:
        ima.measure("app-1", b"c", "execute")
    assert exc.value.code == "IMA_LOG_OVERFLOW"


def test_invalid_xattr_signature_is_rejected():
    ima = ima_appraisal_log.ImaAppraisalLog()
    with pytest.raises(ima_appraisal_log.ImaError) as exc:
        ima.verify_xattr("app-1", signature_valid=False)
    assert exc.value.code == "IMA_XATTR_INVALID"


def test_policy_hash_algorithm_mismatch_is_rejected():
    ima = ima_appraisal_log.ImaAppraisalLog(hash_algo="sha256")
    with pytest.raises(ima_appraisal_log.ImaError) as exc:
        ima.load_policy({"hash_algo": "sha1", "rules": []})
    assert exc.value.code == "IMA_ALGO_MISMATCH"


def test_namespace_mount_event_reaches_main_log():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    ima.measure("mnt-1", b"", "mount", namespace="container")
    entry = ima.log_entries()[0]
    assert entry["namespace"] == "container"
    assert entry["event"] == "mount"


def test_partial_measurement_is_rejected():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    with pytest.raises(ima_appraisal_log.ImaError) as exc:
        ima.measure("app-1", b"half", "write", atomic=False)
    assert exc.value.code == "IMA_PARTIAL_MEASURE"


def test_invalid_policy_batch_leaves_old_policy():
    ima = ima_appraisal_log.ImaAppraisalLog(hash_algo="sha256")
    ima.load_policy({"hash_algo": "sha256", "rules": ["r1"]})
    before = ima.policy_snapshot()
    with pytest.raises(ima_appraisal_log.ImaError) as exc:
        ima.load_policy({"hash_algo": "sha256"})
    assert exc.value.code == "IMA_POLICY_INVALID"
    assert ima.policy_snapshot() == before


def test_policy_update_is_atomic_and_generation_advances():
    ima = ima_appraisal_log.ImaAppraisalLog(hash_algo="sha256")
    first = ima.load_policy({"hash_algo": "sha256", "rules": ["r1", "r2"]})
    second = ima.load_policy({"hash_algo": "sha256", "rules": ["r3"]})
    assert first["loaded"] == 2
    assert second["generation"] == first["generation"] + 1
    assert ima.policy_snapshot()["rules"] == 1


def test_pcr_order_matches_append_log():
    ima = ima_appraisal_log.ImaAppraisalLog(log_size=10)
    ima.measure("app-1", b"a", "create")
    ima.measure("app-1", b"b", "execute")
    sequences = [entry["sequence"] for entry in ima.log_entries()]
    assert sequences == sorted(sequences)
    assert ima.pcr_matches_log() is True
