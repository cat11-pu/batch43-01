import pytest

import reflection_probe_blender as rpb


def make(**kwargs):
    return rpb.ReflectionProbeBlender(**kwargs)


def probe(p, probe_id="p1", position=(0, 0, 0), rotation=(0, 0, 0),
          resolution=128):
    p.add_probe(probe_id, position=position, rotation=rotation,
                box_size=(10, 10, 10), resolution=resolution)
    return probe_id


def test_overlapping_probe_weights_are_normalized():
    p = make()
    probe(p, "p1", (0, 0, 0), resolution=128)
    probe(p, "p2", (4, 0, 0), resolution=128)
    p.capture("p1", 10)
    p.capture("p2", 20)
    out = p.sample((2, 0, 0))
    assert set(out["weights"]) == {"p1", "p2"}
    assert sum(out["weights"].values()) == pytest.approx(1.0)
    assert out["normalized"] is True


def test_boundary_blend_is_continuous_without_hard_switch():
    p = make()
    probe(p, "p1", (0, 0, 0))
    probe(p, "p2", (4, 0, 0))
    p.capture("p1", 10)
    p.capture("p2", 20)
    left = p.sample((2.0, 0, 0))["content"]
    right = p.sample((2.5, 0, 0))["content"]
    assert left == pytest.approx(15.0)
    assert abs(right - left) < 2.0


def test_parallax_uses_the_current_probe_rotation():
    p = make()
    probe(p, "p1")
    p.capture("p1", 10)
    before = p.sample((0, 0, 0), camera=(2, 0, 0))["parallax_local"]
    assert before == pytest.approx((2.0, 0.0, 0.0))
    p.update_transform("p1", rotation=(0, 90, 0))
    after = p.sample((0, 0, 0), camera=(2, 0, 0))["parallax_local"]
    assert after == pytest.approx((0.0, 0.0, 2.0))


def test_parallax_uses_the_camera_that_is_asked_for():
    p = make()
    probe(p, "p1")
    p.capture("p1", 10)
    left = p.sample((0, 0, 0), camera=(1, 0, 0))["parallax_local"]
    right = p.sample((0, 0, 0), camera=(-1, 0, 0))["parallax_local"]
    assert left[0] == pytest.approx(1.0)
    assert right[0] == pytest.approx(-1.0)


def test_camera_inside_probe_is_flagged_and_keeps_direction():
    p = make()
    probe(p, "p1")
    p.capture("p1", 10)
    inside = p.sample((0, 0, 0), camera=(1, 0, 0))
    outside = p.sample((0, 0, 0), camera=(100, 0, 0))
    assert inside["inside_probe"] is True
    assert inside["parallax_local"][0] > 0
    assert outside["inside_probe"] is False
    assert outside["parallax_local"] == pytest.approx((100.0, 0.0, 0.0))


def test_pending_capture_keeps_previous_valid_content():
    p = make()
    probe(p, "p1")
    p.capture("p1", 10)
    token = p.begin_capture("p1")
    assert p.capture_state("p1")["status"] == "pending"
    assert p.sample((0, 0, 0))["content"] == pytest.approx(10.0)
    out = p.finish_capture(token, 20)
    assert out["status"] == "captured"


def test_finished_capture_becomes_visible_to_sampling():
    p = make()
    probe(p, "p1")
    p.capture("p1", 10)
    token = p.begin_capture("p1")
    p.finish_capture(token, 20)
    assert p.capture_state("p1")["status"] == "ready"
    assert p.sample((0, 0, 0))["content"] == pytest.approx(20.0)


def test_removing_probe_cancels_pending_capture_callback():
    p = make()
    probe(p, "p1")
    token = p.begin_capture("p1")
    p.remove_probe("p1")
    out = p.finish_capture(token, 99)
    assert out["status"] == "cancelled"
    assert token["token"] not in [row["token"] for row in p.callbacks()]


def test_cancelled_capture_token_cannot_write_content():
    p = make()
    probe(p, "p1")
    p.capture("p1", 10)
    token = p.begin_capture("p1")
    p.cancel_capture(token)
    out = p.finish_capture(token, 99)
    assert out["status"] == "cancelled"
    assert p.sample((0, 0, 0))["content"] == pytest.approx(10.0)


def test_probe_limit_raises_instead_of_silently_dropping():
    p = make(max_probes=1)
    probe(p, "p1")
    with pytest.raises(rpb.ProbeError) as exc:
        p.add_probe("p2", box_size=(10, 10, 10))
    assert exc.value.code == "probe_limit"
    assert set(p.sample((0, 0, 0))["weights"]) == {"p1"}


def test_recapture_updates_content_and_version():
    p = make()
    probe(p, "p1")
    first = p.capture("p1", 5)
    second = p.capture("p1", 9)
    assert second["version"] > first["version"]
    assert p.capture_state("p1")["version"] == second["version"]
    assert p.sample((0, 0, 0))["content"] == pytest.approx(9.0)


def test_sharpness_blends_across_probe_resolutions():
    p = make()
    probe(p, "p1", resolution=128)
    probe(p, "p2", (4, 0, 0), resolution=512)
    p.capture("p1", 10)
    p.capture("p2", 10)
    out = p.sample((2, 0, 0))
    assert out["sharpness"] == pytest.approx(320.0)


def test_removed_probe_is_no_longer_blended():
    p = make()
    probe(p, "p1")
    probe(p, "p2", (4, 0, 0))
    p.capture("p1", 10)
    p.capture("p2", 20)
    p.remove_probe("p1")
    out = p.sample((2, 0, 0))
    assert set(out["weights"]) == {"p2"}
    assert out["content"] == pytest.approx(20.0)


def test_device_restore_keeps_last_valid_content():
    p = make()
    probe(p, "p1")
    p.capture("p1", 42)
    p.device_lost()
    assert p.device_state() == "lost"
    p.device_restored()
    assert p.device_state() == "ok"
    assert p.sample((0, 0, 0))["content"] == pytest.approx(42.0)


def test_stale_async_capture_cannot_overwrite_newer_content():
    p = make()
    probe(p, "p1")
    p.capture("p1", 10)
    token = p.begin_capture("p1")
    p.capture("p1", 99)
    out = p.finish_capture(token, 1000)
    assert out["status"] == "stale"
    assert p.sample((0, 0, 0))["content"] == pytest.approx(99.0)
