import pytest

import frame_command_fence as fcf


def make(**kwargs):
    return fcf.FrameCommandFence(**kwargs)


def recorded(p, frame=0):
    return p.begin(frame)["token"]


def test_submit_in_recording_order_succeeds():
    p = make()
    recorded(p, 0)
    first = p.submit(0)
    recorded(p, 1)
    second = p.submit(1)
    assert first["status"] == "submitted"
    assert second["status"] == "submitted"
    assert p.pending() == [0, 1]


def test_out_of_order_submit_is_rejected():
    p = make()
    recorded(p, 0)
    recorded(p, 1)
    with pytest.raises(fcf.FenceError) as exc:
        p.submit(1)
    assert exc.value.code == "out_of_order"
    assert p.submit(0)["status"] == "submitted"


def test_recycle_before_fence_completion_raises():
    p = make()
    recorded(p, 0)
    p.submit(0)
    p.acquire("constant-buffer", 0)
    with pytest.raises(fcf.ResourceInUseError):
        p.recycle("constant-buffer")


def test_recycle_after_signal_is_allowed():
    p = make()
    recorded(p, 0)
    p.submit(0)
    p.acquire("constant-buffer", 0)
    p.signal(0)
    assert p.wait(0)["status"] == "complete"
    assert p.recycle("constant-buffer")["status"] == "recycled"


def test_wait_timeout_keeps_resource_locked():
    p = make()
    recorded(p, 0)
    p.submit(0)
    p.acquire("constant-buffer", 0)
    out = p.wait(0, timeout=0)
    assert out["status"] == "timeout"
    with pytest.raises(fcf.ResourceInUseError):
        p.recycle("constant-buffer")


def test_signal_then_wait_completes_the_frame():
    p = make()
    recorded(p, 0)
    p.submit(0)
    assert p.wait(0, timeout=0)["status"] == "timeout"
    p.signal(0)
    assert p.wait(0, timeout=0)["status"] == "complete"
    assert p.pending() == []


def test_resize_rebuilds_in_flight_frames_and_bumps_generation():
    p = make()
    recorded(p, 0)
    p.submit(0)
    before = p.state()["generation"]
    out = p.resize(1280, 720)
    assert out["generation"] == before + 1
    assert out["cancelled"] == [0]
    assert p.pending() == []
    recorded(p, 0)
    assert p.submit(0)["status"] == "submitted"


def test_old_generation_token_is_dropped_after_resize():
    p = make()
    old = recorded(p, 0)
    p.submit(0)
    p.resize(1280, 720)
    recorded(p, 0)
    with pytest.raises(fcf.FenceError) as exc:
        p.submit(0, token=old)
    assert exc.value.code == "stale_generation"


def test_device_restore_clears_queue_and_rebuilds():
    p = make()
    recorded(p, 0)
    p.submit(0)
    p.device_lost()
    assert p.state()["device"] == "lost"
    assert p.pending() == []
    p.device_restored(1920, 1080)
    assert p.state()["device"] == "ok"
    recorded(p, 0)
    assert p.submit(0)["status"] == "submitted"


def test_same_resource_cannot_be_acquired_by_two_in_flight_frames():
    p = make()
    for frame in (0, 1):
        recorded(p, frame)
        p.submit(frame)
    p.acquire("upload-buffer", 0)
    with pytest.raises(fcf.FenceError) as exc:
        p.acquire("upload-buffer", 1)
    assert exc.value.code == "resource_in_use"


def test_shutdown_completes_signalled_frames_and_cancels_the_rest():
    p = make()
    recorded(p, 0)
    p.submit(0)
    p.signal(0)
    recorded(p, 1)
    p.submit(1)
    out = p.shutdown(timeout=0)
    assert out["completed"] == [0]
    assert out["cancelled"] == [1]
    assert p.pending() == []


def test_shutdown_without_in_flight_work_is_clean():
    p = make()
    out = p.shutdown(timeout=0)
    assert out["completed"] == []
    assert out["cancelled"] == []
    assert p.pending() == []


def test_too_many_in_flight_frames_are_rejected():
    p = make(frames_in_flight=2)
    for frame in (0, 1):
        recorded(p, frame)
        p.submit(frame)
    with pytest.raises(fcf.FenceError) as exc:
        p.begin(2)
    assert exc.value.code == "too_many_frames"


def test_pending_excludes_signalled_frames_and_keeps_order():
    p = make(frames_in_flight=3)
    for frame in (0, 1, 2):
        recorded(p, frame)
        p.submit(frame)
    p.signal(1)
    assert p.pending() == [0, 2]


def test_wait_on_unsubmitted_frame_is_rejected():
    p = make()
    recorded(p, 0)
    with pytest.raises(fcf.FenceError) as exc:
        p.wait(0, timeout=0)
    assert exc.value.code == "not_submitted"
