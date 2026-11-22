import pytest

import simulcast_lane_select as sls


def make(**kwargs):
    return sls.SimulcastLaneSelect(**kwargs)


def sub(p, subscriber="viewer-1", stream="camera", layer="low"):
    p.subscribe(subscriber, stream=stream, layer=layer)
    return subscriber


def test_select_sets_pending_and_keeps_old_layer_displayed():
    p = make()
    sub(p)
    out = p.select("viewer-1", "camera", "high", now=0)
    assert out["status"] == "pending"
    current = p.current("viewer-1", "camera")
    assert current["active"] == "low"
    assert current["desired"] == "high"
    assert current["display"] == "low"


def test_keyframe_apply_switches_atomically():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    out = p.apply("viewer-1", "camera", "high", frame=100, now=1)
    assert out["status"] == "switched"
    assert p.current("viewer-1", "camera")["active"] == "high"
    assert p.current("viewer-1", "camera")["display"] == "high"
    assert p.current("viewer-1", "camera")["pending"] is False


def test_repeated_select_of_same_layer_does_not_request_more_keyframes():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    before = p.current("viewer-1", "camera")["keyframe_requests"]
    out = p.select("viewer-1", "camera", "high", now=0.5)
    assert out["status"] == "same_layer"
    assert p.current("viewer-1", "camera")["keyframe_requests"] == before


def test_same_frame_keyframe_request_is_deduplicated():
    p = make()
    sub(p)
    first = p.request_keyframe("viewer-1", "camera", frame=10, now=5)
    second = p.request_keyframe("viewer-1", "camera", frame=10, now=5.5)
    assert first["status"] == "requested"
    assert second["status"] == "duplicate"


def test_different_frame_inside_one_second_is_throttled():
    p = make()
    sub(p)
    p.request_keyframe("viewer-1", "camera", frame=10, now=5)
    out = p.request_keyframe("viewer-1", "camera", frame=11, now=5.5)
    assert out["status"] == "throttled"


def test_keyframe_request_allowed_after_one_second():
    p = make()
    sub(p)
    p.request_keyframe("viewer-1", "camera", frame=10, now=5)
    out = p.request_keyframe("viewer-1", "camera", frame=11, now=6)
    assert out["status"] == "requested"


def test_two_subscribers_keep_independent_layers():
    p = make()
    sub(p, "viewer-1", "camera", "low")
    sub(p, "viewer-2", "camera", "low")
    p.select("viewer-1", "camera", "high", now=0)
    p.select("viewer-2", "camera", "mid", now=0)
    p.apply("viewer-1", "camera", "high", frame=1, now=1)
    p.apply("viewer-2", "camera", "mid", frame=1, now=1)
    assert p.current("viewer-1", "camera")["active"] == "high"
    assert p.current("viewer-2", "camera")["active"] == "mid"


def test_keyframe_from_unrequested_layer_is_rejected():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    with pytest.raises(sls.LaneError) as exc:
        p.apply("viewer-1", "camera", "mid", frame=99, now=1)
    assert exc.value.code == "unexpected_layer"
    assert p.current("viewer-1", "camera")["active"] == "low"


def test_non_keyframe_does_not_complete_the_switch():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    out = p.apply("viewer-1", "camera", "high", frame=99, keyframe=False,
                  now=1)
    assert out["status"] == "waiting_keyframe"
    assert p.current("viewer-1", "camera")["active"] == "low"


def test_reconnect_keeps_last_applied_layer():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    p.apply("viewer-1", "camera", "high", frame=1, now=1)
    p.reconnect("viewer-1", "camera", now=2)
    current = p.current("viewer-1", "camera")
    assert current["active"] == "high"
    assert current["desired"] == "high"


def test_reconnect_during_pending_keeps_old_layer():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    p.reconnect("viewer-1", "camera", now=1)
    current = p.current("viewer-1", "camera")
    assert current["active"] == "low"
    assert current["desired"] == "low"
    assert current["pending"] is False


def test_apply_before_publisher_restore_is_rejected():
    p = make()
    sub(p)
    p.publisher_restart(now=1)
    with pytest.raises(sls.LaneError) as exc:
        p.apply("viewer-1", "camera", "low", frame=1, now=2)
    assert exc.value.code == "not_subscribed"


def test_restore_recovers_persisted_subscription_state():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    p.apply("viewer-1", "camera", "high", frame=1, now=1)
    state = p.snapshot()
    p.publisher_restart(now=2)
    p.restore(state)
    current = p.current("viewer-1", "camera")
    assert current["active"] == "high"
    assert current["desired"] == "high"


def test_camera_and_screen_streams_are_independent():
    p = make()
    sub(p, "viewer-1", "camera", "low")
    sub(p, "viewer-1", "screen", "high")
    p.select("viewer-1", "camera", "high", now=0)
    assert p.current("viewer-1", "screen")["active"] == "high"
    assert p.current("viewer-1", "camera")["active"] == "low"


def test_late_frame_from_old_layer_does_not_switch_back():
    p = make()
    sub(p)
    p.select("viewer-1", "camera", "high", now=0)
    p.apply("viewer-1", "camera", "high", frame=1, now=1)
    p.select("viewer-1", "camera", "mid", now=2)
    with pytest.raises(sls.LaneError) as exc:
        p.apply("viewer-1", "camera", "high", frame=2, now=3)
    assert exc.value.code == "unexpected_layer"
    assert p.current("viewer-1", "camera")["active"] == "high"
