import pytest

import resolution_budget_governor as rbg


def make(**kwargs):
    return rbg.ResolutionBudgetGovernor(**kwargs)


def feed(p, frame_ms, count=5, gpu_ms=None, view="main"):
    for _ in range(count):
        p.sample(frame_time_ms=frame_ms, gpu_ms=gpu_ms, view=view)


def test_single_slow_frame_does_not_change_scale():
    p = make()
    feed(p, 40.0, count=1)
    out = p.decide()
    assert out["changed"] is False
    assert p.scale() == 1.0


def test_sustained_slow_frames_downscale_one_step():
    p = make()
    feed(p, 40.0, count=5)
    out = p.decide()
    assert out["changed"] is True
    assert p.scale() == 0.7
    assert p.state()["samples"] == 0


def test_alternating_frames_do_not_flap_between_scales():
    p = make()
    for frame_ms in (5.0, 25.0, 5.0, 25.0, 5.0):
        p.sample(frame_time_ms=frame_ms)
    for _ in range(4):
        assert p.decide()["changed"] is False
        assert p.scale() == 1.0


def test_upscale_needs_sustained_fast_frames():
    p = make()
    feed(p, 40.0, count=5)
    p.decide()
    assert p.scale() == 0.7
    feed(p, 8.0, count=5)
    assert p.decide()["changed"] is False
    assert p.scale() == 0.7
    feed(p, 8.0, count=5)
    assert p.decide()["changed"] is True
    assert p.scale() == 1.0


def test_missing_gpu_timing_uses_conservative_single_step():
    p = make()
    feed(p, 40.0, count=5, gpu_ms=None)
    out = p.decide()
    assert out["changed"] is True
    assert p.scale() == 0.7
    assert p.state()["gpu_timing"] == "missing"


def test_ui_stays_at_native_resolution_when_downscaled():
    p = make()
    p.apply(0.5)
    assert p.ui_size() == (1920, 1080)
    assert p.render_size() == (960, 540)


def test_screenshot_is_always_native_resolution():
    p = make()
    p.apply(0.5)
    shot = p.capture("screenshot")
    assert (shot["width"], shot["height"]) == (1920, 1080)


def test_video_capture_is_always_native_resolution():
    p = make()
    p.apply(0.7)
    video = p.capture("video")
    assert (video["width"], video["height"]) == (1920, 1080)
    assert video["kind"] == "video"


def test_apply_rebuilds_every_size_dependent_buffer():
    p = make()
    p.register_buffer("dof", 1920, 1080)
    p.apply(0.5)
    for name in ("color", "depth", "post", "dof"):
        assert p.buffers()[name] == {"width": 960, "height": 540}


def test_callback_from_old_generation_is_ignored():
    p = make()
    first = p.apply(0.5)
    second = p.apply(0.7)
    out = p.late_callback(first["generation"], "color", 960, 540)
    assert out["status"] == "ignored"
    assert p.buffers()["color"] == {"width": 1344, "height": 756}
    assert second["generation"] > first["generation"]


def test_resume_after_background_reenters_evaluation():
    p = make()
    feed(p, 40.0, count=5)
    p.decide()
    assert p.scale() == 0.7
    p.enter_background()
    p.resume()
    assert p.state()["needs_evaluation"] is True
    feed(p, 40.0, count=5)
    assert p.decide()["changed"] is True
    assert p.scale() == 0.5


def test_thermal_limit_forces_a_downscale():
    p = make()
    feed(p, 8.0, count=5)
    p.set_thermal("high")
    out = p.decide()
    assert out["changed"] is True
    assert p.scale() == 0.7


def test_reset_restores_default_scale_and_clears_samples():
    p = make()
    feed(p, 40.0, count=5)
    p.decide()
    p.apply(0.5)
    p.reset()
    assert p.scale() == 1.0
    assert p.state()["samples"] == 0
    assert p.buffers()["color"] == {"width": 1920, "height": 1080}


def test_pip_sampling_does_not_drag_down_the_main_view():
    p = make()
    feed(p, 80.0, count=10, view="pip")
    out = p.decide()
    assert out["changed"] is False
    assert p.scale() == 1.0


def test_scale_never_jumps_more_than_one_step_per_decision():
    p = make()
    feed(p, 200.0, count=5)
    p.decide()
    assert p.scale() == 0.7
    feed(p, 200.0, count=5)
    p.decide()
    assert p.scale() == 0.5
