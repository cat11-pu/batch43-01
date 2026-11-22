import pytest

import audio_seek_preroll as asp


def make(**kwargs):
    kwargs.setdefault("sample_rate", 48000)
    kwargs.setdefault("encoder_delay", 1024)
    kwargs.setdefault("tail_padding", 512)
    kwargs.setdefault("preroll_ms", 50)
    return asp.AudioSeekPreroll(**kwargs)


def track(p, track_id="t1", seconds=10.0):
    p.load(track_id, frames=int(seconds * p.sample_rate))
    return track_id


def test_seek_decodes_from_a_position_before_the_target():
    p = make()
    track(p)
    out = p.seek("t1", 5000)
    assert out["target_frame"] == 240000
    assert out["start_frame"] < out["target_frame"]
    assert out["start_frame"] <= out["target_frame"] - 2000


def test_preroll_samples_are_reported_as_discarded():
    p = make()
    track(p)
    out = p.seek("t1", 5000)
    assert out["discard_frames"] == out["target_frame"] - out["start_frame"]
    assert out["discard_frames"] >= 2000


def test_first_delivered_sample_is_the_target_frame():
    p = make()
    track(p)
    out = p.seek("t1", 5000)
    samples = p.read(out["request_id"], 8)
    assert samples[0] == out["target_frame"] % 1000
    assert samples == [(out["target_frame"] + i) % 1000 for i in range(8)]


def test_seek_to_zero_delivers_the_first_sample():
    p = make()
    track(p)
    out = p.seek("t1", 0)
    assert out["start_frame"] == 0
    assert out["discard_frames"] == 0
    assert p.read(out["request_id"], 3)[0] == 0


def test_vbr_duration_comes_from_the_frame_index_not_bitrate():
    p = make()
    p.load("vbr", frames=48000, vbr=True, size_bytes=100000, bitrate=128000)
    assert p.duration_ms("vbr") == pytest.approx(1000.0)
    p.load("vbr2", frames=96000, vbr=True, size_bytes=100000, bitrate=128000)
    assert p.duration_ms("vbr2") == pytest.approx(2000.0)


def test_seek_near_the_end_keeps_the_final_samples():
    p = make()
    track(p, seconds=3.0)
    out = p.seek("t1", 2900)
    assert out["end_frame"] == p.total_frames("t1")
    remaining = out["end_frame"] - out["target_frame"]
    samples = p.read(out["request_id"], remaining + 100)
    assert len(samples) == remaining
    assert samples[-1] == (out["end_frame"] - 1) % 1000


def test_new_seek_invalidates_the_previous_request():
    p = make()
    track(p)
    old = p.seek("t1", 1000)
    new = p.seek("t1", 2000)
    assert old["request_id"] != new["request_id"]
    assert p.active_request("t1") == new["request_id"]


def test_reading_a_stale_request_is_rejected():
    p = make()
    track(p)
    old = p.seek("t1", 1000)
    p.seek("t1", 2000)
    with pytest.raises(asp.AudioError) as exc:
        p.read(old["request_id"], 4)
    assert exc.value.code == "stale_request"


def test_cancel_invalidates_the_request():
    p = make()
    track(p)
    out = p.seek("t1", 1000)
    p.cancel(out["request_id"])
    with pytest.raises(asp.AudioError) as exc:
        p.read(out["request_id"], 4)
    assert exc.value.code == "stale_request"


def test_loop_points_are_snapped_to_sample_boundaries():
    p = make()
    track(p)
    out = p.loop_points("t1", 10.0005, 20.0)
    assert isinstance(out["start_sample"], int)
    assert isinstance(out["end_sample"], int)
    assert out["start_sample"] == 480


def test_loop_end_excludes_tail_padding():
    p = make()
    track(p)
    out = p.loop_points("t1", 0.0, 10000.0)
    assert out["end_sample"] == p.total_frames("t1") - 1024 - 512
    assert out["start_sample"] == 0


def test_loop_with_start_after_end_is_rejected():
    p = make()
    track(p)
    with pytest.raises(asp.AudioError) as exc:
        p.loop_points("t1", 5000.0, 4000.0)
    assert exc.value.code == "invalid_loop"


def test_gapless_playlist_has_no_gap_between_tracks():
    p = make()
    track(p, "t1", seconds=2.0)
    track(p, "t2", seconds=2.0)
    assert p.playlist_gap("t1", "t2") == 0


def test_output_format_change_renegotiates():
    p = make()
    assert p.output_state()["channels"] == 2
    out = p.set_output(1)
    assert out["status"] == "renegotiated"
    assert p.output_state()["channels"] == 1
    assert p.set_output(1)["status"] == "unchanged"


def test_long_pause_requires_a_fresh_decode():
    p = make()
    track(p)
    p.seek("t1", 1000)
    p.pause(now_ms=0)
    assert p.resume(now_ms=60001)["status"] == "redecode_required"
    p.pause(now_ms=0)
    assert p.resume(now_ms=1000)["status"] == "resumed"
