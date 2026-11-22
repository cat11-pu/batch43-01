import pytest

import srtp_media_replay_ledger


def _stream(ledger, ssrc=0x1111, source_id="cam-1", key_id="k0", epoch=0):
    return ledger.open_stream(ssrc, source_id=source_id, key_id=key_id, epoch=epoch)


def test_same_stream_and_index_is_replay():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    assert ledger.accept(0x1111, 5) is True
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 5)
    assert exc.value.code == "SRTP_REPLAY"


def test_sequence_rollover_65535_to_zero_accepted_once():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    assert ledger.accept(0x1111, 65535) is True
    assert ledger.accept(0x1111, 0) is True
    assert ledger.stats(0x1111)["rollovers"] == 1


def test_replay_after_rollover_does_not_advance_rollover_count():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    ledger.accept(0x1111, 65535)
    ledger.accept(0x1111, 0)
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 65535)
    assert exc.value.code == "SRTP_REPLAY"
    assert ledger.stats(0x1111)["rollovers"] == 1
    assert ledger.accept(0x1111, 1) is True


def test_out_of_order_packet_inside_window_accepted():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger(window=16)
    _stream(ledger)
    assert ledger.accept(0x1111, 10) is True
    assert ledger.accept(0x1111, 8) is True
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 8)
    assert exc.value.code == "SRTP_REPLAY"


def test_packet_far_below_window_rejected():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger(window=16)
    _stream(ledger)
    ledger.accept(0x1111, 100)
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 1)
    assert exc.value.code == "SRTP_REPLAY"


def test_control_packets_have_separate_replay_window():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    assert ledger.accept(0x1111, 5) is True
    assert ledger.accept(0x1111, 5, is_control=True) is True
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 5, is_control=True)
    assert exc.value.code == "SRTP_REPLAY"


def test_control_replay_does_not_block_media_flow():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    assert ledger.accept(0x1111, 1, is_control=True) is True
    with pytest.raises(srtp_media_replay_ledger.SrtpError):
        ledger.accept(0x1111, 1, is_control=True)
    assert ledger.accept(0x1111, 2) is True


def test_control_packet_with_wrong_key_id_rejected():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger, key_id="media-key")
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 1, key_id="control-key", is_control=True)
    assert exc.value.code == "SRTP_KEY_ID_MISMATCH"


def test_four_byte_truncated_auth_tag_rejected():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 1, tag_len=4)
    assert exc.value.code == "SRTP_TAG_TOO_SHORT"


def test_old_epoch_rejected_after_atomic_key_rotation():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    ledger.accept(0x1111, 1, epoch=0)
    ledger.rotate_key(0x1111, 1)
    assert ledger.active_epoch(0x1111) == 1
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 2, epoch=0)
    assert exc.value.code == "SRTP_KEY_EXPIRED"
    assert ledger.accept(0x1111, 0, epoch=1) is True


def test_key_packet_limit_forces_rekey():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger(max_packets_per_key=2)
    _stream(ledger)
    assert ledger.accept(0x1111, 1) is True
    assert ledger.accept(0x1111, 2) is True
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.accept(0x1111, 3)
    assert exc.value.code == "SRTP_KEY_EXPIRED"


def test_new_epoch_index_space_restarts():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    ledger.accept(0x1111, 10)
    ledger.rotate_key(0x1111, 1)
    assert ledger.accept(0x1111, 0, epoch=1) is True


def test_conflicting_ssrc_source_id_rejected():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    ledger.open_stream(0x2222, source_id="endpoint-a")
    with pytest.raises(srtp_media_replay_ledger.SrtpError) as exc:
        ledger.open_stream(0x2222, source_id="endpoint-b")
    assert exc.value.code == "SRTP_SSRC_COLLISION"


def test_stats_track_accepted_and_rejected_counts():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    _stream(ledger)
    ledger.accept(0x1111, 1)
    ledger.accept(0x1111, 2)
    with pytest.raises(srtp_media_replay_ledger.SrtpError):
        ledger.accept(0x1111, 2)
    stats = ledger.stats(0x1111)
    assert stats["accepted"] == 2
    assert stats["rejected"] == 1


def test_two_streams_keep_independent_replay_state():
    ledger = srtp_media_replay_ledger.SrtpMediaReplayLedger()
    ledger.open_stream(0x3333, source_id="a")
    ledger.open_stream(0x4444, source_id="b")
    assert ledger.accept(0x3333, 9) is True
    assert ledger.accept(0x4444, 9) is True
    with pytest.raises(srtp_media_replay_ledger.SrtpError):
        ledger.accept(0x3333, 9)
    assert ledger.accept(0x4444, 10) is True
