import pytest

import video_frame_presenter as vfp


def make(**kwargs):
    return vfp.VideoFramePresenter(**kwargs)


def test_each_24fps_frame_is_presented_once():
    p = make(refresh_hz=60.0)
    for index in range(3):
        p.submit(f"f{index}", index / 24.0)
    ids = []
    for step in range(3):
        p.set_audio_clock(step / 24.0)
        out = p.present()
        ids.append(out["frame_id"])
    assert ids == ["f0", "f1", "f2"]


def test_video_does_not_run_ahead_of_audio_clock():
    p = make()
    p.submit("f1", 1.0)
    p.set_audio_clock(0.5)
    assert p.present()["frame_id"] is None


def test_dropped_frame_does_not_trigger_catch_up():
    p = make(capacity=3)
    for index in range(1, 5):
        p.submit(f"f{index}", float(index))
    assert [pts for pts in p.queue()] == [2.0, 3.0, 4.0]
    p.set_audio_clock(4.0)
    assert [p.present()["pts"] for _ in range(3)] == [2.0, 3.0, 4.0]


def test_seek_resets_video_queue_and_audio_position():
    p = make()
    p.submit("old", 1.0)
    p.set_audio_clock(1.0)
    p.present()
    out = p.seek(10.0)
    assert out["pts"] == 10.0
    assert p.queue() == []
    assert p.state()["audio_clock"] == 10.0
    p.submit("new", 10.0)
    p.set_audio_clock(10.0)
    assert p.present()["frame_id"] == "new"


def test_refresh_change_keeps_presentation_monotonic():
    p = make(refresh_hz=60.0)
    p.submit("f1", 0.0)
    p.set_audio_clock(0.0)
    p.present()
    p.set_refresh(30.0)
    p.submit("f2", 0.5)
    p.set_audio_clock(0.5)
    second = p.present()["pts"]
    assert second > 0.0
    assert p.presented() == [0.0, 0.5]


def test_pause_freezes_current_frame():
    p = make()
    p.submit("f1", 0.0)
    p.set_audio_clock(0.0)
    p.present()
    p.pause()
    p.submit("f2", 1.0)
    p.set_audio_clock(1.0)
    out = p.present()
    assert out["frame_id"] == "f1"
    assert out["paused"] is True
    p.resume()
    p.set_audio_clock(1.0)
    assert p.present()["frame_id"] == "f2"


def test_full_queue_drops_oldest_frame_not_newest():
    p = make(capacity=3)
    p.submit("f1", 1.0)
    p.submit("f2", 2.0)
    p.submit("f3", 3.0)
    out = p.submit("f4", 4.0)
    assert out["dropped"] == "f1"
    assert p.queue() == [2.0, 3.0, 4.0]


def test_wall_clock_does_not_advance_presentation():
    p = make()
    p.submit("f1", 5.0)
    p.set_audio_clock(0.0)
    assert p.present(now=999.0)["frame_id"] is None


def test_picture_in_picture_has_independent_state():
    main = make()
    pip = main.create_view("pip")
    pip.seek(100.0)
    assert main.state()["audio_clock"] == 0.0
    assert pip.state()["audio_clock"] == 100.0


def test_hdr_rebuild_keeps_timeline_continuous():
    p = make()
    p.submit("f1", 0.0)
    p.set_audio_clock(0.0)
    p.present()
    out = p.set_hdr(True)
    assert out["rebuild"] is True
    p.submit("f2", 0.1)
    p.set_audio_clock(0.1)
    assert p.present()["pts"] > 0.0


def test_late_frame_is_rejected():
    p = make()
    p.submit("f2", 2.0)
    p.set_audio_clock(2.0)
    p.present()
    with pytest.raises(vfp.PresenterError) as exc:
        p.submit("f1", 1.0)
    assert exc.value.code == "LATE_FRAME"


def test_latency_change_adjusts_audio_offset():
    p = make()
    p.submit("f1", 1.05)
    p.set_audio_clock(1.0)
    assert p.present()["frame_id"] is None
    p.set_audio_latency(0.1)
    assert p.present()["frame_id"] == "f1"


def test_presented_timestamps_are_strictly_increasing():
    p = make()
    for index in range(3):
        p.submit(f"f{index}", index * 0.1)
    for step in range(3):
        p.set_audio_clock(step * 0.1)
        p.present()
    stamps = p.presented()
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_audio_video_drift_after_seek_is_within_one_frame():
    p = make(refresh_hz=60.0)
    p.seek(10.0)
    p.submit("f1", 10.0)
    p.set_audio_clock(10.0)
    out = p.present()
    assert abs(out["pts"] - p.state()["audio_clock"]) <= 1.0 / 60.0


def test_seek_is_one_atomic_operation():
    p = make()
    p.submit("f1", 1.0)
    p.submit("f2", 2.0)
    out = p.seek(7.0)
    assert out["queue"] == []
    assert p.queue() == []
    assert p.state()["audio_clock"] == 7.0
