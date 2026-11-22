import pytest

import ptp_spoof_guard


def test_forged_announce_source_is_rejected_and_audited():
    guard = ptp_spoof_guard.PtpSpoofGuard(allowed_sources=("10.0.0.1",))
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.process_announce("10.9.9.9", priority1=1)
    assert exc.value.code == "PTP_ANNOUNCE_REJECTED"
    entries = guard.audit_log()
    assert any(e["type"] == "announce_reject" and e["source"] == "10.9.9.9"
               for e in entries)


def test_forged_best_priority_cannot_become_master():
    guard = ptp_spoof_guard.PtpSpoofGuard(
        allowed_sources=("10.0.0.1", "10.0.0.2"))
    guard.process_announce("10.0.0.2", priority1=10)
    with pytest.raises(ptp_spoof_guard.PtpError):
        guard.process_announce("10.9.9.9", priority1=1)
    assert guard.best_master() == "10.0.0.2"


def test_better_allowed_announce_becomes_master():
    guard = ptp_spoof_guard.PtpSpoofGuard(
        allowed_sources=("10.0.0.1", "10.0.0.2"))
    guard.process_announce("10.0.0.1", priority1=20)
    result = guard.process_announce("10.0.0.2", priority1=5)
    assert result["selected"] is True
    assert guard.best_master() == "10.0.0.2"


def test_duplicate_sync_sequence_is_replay():
    guard = ptp_spoof_guard.PtpSpoofGuard()
    guard.process_sync("10.0.0.1", 7)
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.process_sync("10.0.0.1", 7)
    assert exc.value.code == "PTP_SYNC_REPLAY"


def test_follow_up_without_sync_is_replay():
    guard = ptp_spoof_guard.PtpSpoofGuard()
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.process_follow_up("10.0.0.1", 7, 100)
    assert exc.value.code == "PTP_SYNC_REPLAY"


def test_sync_follow_up_pairs_only_once():
    guard = ptp_spoof_guard.PtpSpoofGuard()
    guard.process_sync("10.0.0.1", 8)
    paired = guard.process_follow_up("10.0.0.1", 8, 1234)
    assert paired["paired"] is True
    assert paired["origin_timestamp"] == 1234
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.process_follow_up("10.0.0.1", 8, 9999)
    assert exc.value.code == "PTP_SYNC_REPLAY"


def test_correction_accumulation_over_limit_is_rejected():
    guard = ptp_spoof_guard.PtpSpoofGuard(correction_limit_ns=1000000)
    guard.apply_correction(1, 600000)
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.apply_correction(1, 600000)
    assert exc.value.code == "PTP_CORRECTION_LIMIT"


def test_correction_within_limit_accumulates():
    guard = ptp_spoof_guard.PtpSpoofGuard(correction_limit_ns=1000000)
    assert guard.apply_correction(1, 300000)["cumulative_ns"] == 300000
    assert guard.apply_correction(1, 200000)["cumulative_ns"] == 500000


def test_unicast_request_from_unknown_source_is_rejected():
    guard = ptp_spoof_guard.PtpSpoofGuard(allowed_sources=("10.0.0.1",))
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.request_unicast("198.51.100.7", duration=60)
    assert exc.value.code == "PTP_UNICAST_SOURCE_INVALID"


def test_timestamp_queue_overflow_is_reported():
    guard = ptp_spoof_guard.PtpSpoofGuard(queue_limit=2)
    guard.enqueue_timestamp(1)
    guard.enqueue_timestamp(2)
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.enqueue_timestamp(3)
    assert exc.value.code == "PTP_QUEUE_OVERFLOW"


def test_clock_step_is_audited_and_alarmed():
    guard = ptp_spoof_guard.PtpSpoofGuard()
    result = guard.audit_step(offset_ns=5000)
    assert result["audited"] is True
    assert result["alarm"] is True
    assert guard.audit_log()[-1]["type"] == "step"
    assert "PTP_STEP_ALARM" in guard.alarms()


def test_leap_second_is_audited_and_alarmed():
    guard = ptp_spoof_guard.PtpSpoofGuard()
    result = guard.audit_leap(seconds=1)
    assert result["alarm"] is True
    assert guard.audit_log()[-1]["type"] == "leap"
    assert "PTP_LEAP_ALARM" in guard.alarms()


def test_audit_entries_include_master_identity_and_source():
    guard = ptp_spoof_guard.PtpSpoofGuard(allowed_sources=("10.0.0.1",))
    guard.process_announce("10.0.0.1", priority1=10)
    guard.audit_step(offset_ns=10)
    entry = guard.audit_log()[-1]
    assert entry["master_id"] == "10.0.0.1"
    assert entry["source"] == "10.0.0.1"


def test_stale_restored_offset_is_not_applied():
    guard = ptp_spoof_guard.PtpSpoofGuard(max_offset_age_s=300)
    result = guard.restore_offset({"offset_ns": 120, "captured_at": 100},
                                  now=1000)
    assert result["applied"] is False


def test_config_template_mismatch_is_rejected():
    guard = ptp_spoof_guard.PtpSpoofGuard()
    with pytest.raises(ptp_spoof_guard.PtpError) as exc:
        guard.verify_config(config_version="v1", expected_version="v2")
    assert exc.value.code == "PTP_CONFIG_MISMATCH"
