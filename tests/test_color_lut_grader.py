import math

import pytest

import color_lut_grader as clg


def make(**kwargs):
    return clg.ColorLutGrader(**kwargs)


def lut(axis="r", size=2, invert=False):
    entries = []
    for r in range(size):
        for g in range(size):
            for b in range(size):
                value = {"r": r, "g": g, "b": b}[axis] / (size - 1)
                if invert:
                    value = 1.0 - value
                entries.append(value)
    return entries


def loaded(size=2, axis="r", invert=False, domain=(0.0, 1.0), version=1,
           flip=False):
    g = make()
    g.load_lut(lut(axis, size, invert), size, domain=domain,
               version=version, flip=flip)
    return g


def test_identity_lut_round_trips_color():
    g = loaded(axis="r")
    out = g.grade((0.5, 0.25, 0.75))
    assert all(abs(channel - 0.5) < 1e-9 for channel in out)


def test_blue_axis_interpolation_uses_correct_weights():
    g = loaded(axis="b")
    out = g.grade((0.0, 0.0, 0.25))
    assert abs(out[0] - 0.25) < 1e-9


def test_lut_size_mismatch_is_rejected():
    g = make()
    with pytest.raises(clg.LutError) as exc:
        g.load_lut(lut("r", 2), 3)
    assert exc.value.code == "LUT_SIZE_MISMATCH"


def test_edge_sampling_does_not_bleed_neighbor_layer():
    g = loaded(axis="r")
    out = g.sample((0.0, 0.99, 0.99))
    assert out == 0.0


def test_hdr_values_above_one_are_not_clipped():
    g = loaded(axis="r", domain=(0.0, 2.0))
    out = g.grade((1.5, 1.5, 1.5))
    assert abs(out[0] - 0.75) < 1e-9


def test_strength_blends_in_linear_space():
    g = loaded(axis="r", invert=True)
    g.set_strength(0.5)
    out = g.grade((0.25, 0.25, 0.25))
    assert abs(out[0] - 0.5) < 1e-9


def test_render_applies_lut_to_scene_only():
    g = loaded(axis="r", invert=True)
    out = g.render([(0.25, 0.25, 0.25)], ui_colors=[(0.25, 0.25, 0.25)])
    assert abs(out["scene"][0][0] - 0.75) < 1e-9
    assert out["ui"][0] == (0.25, 0.25, 0.25)


def test_hot_reload_rejects_stale_lut_version():
    g = loaded(version=1)
    old = g.lut_state()["version"]
    g.reload(lut("r", 2), 2, version=2)
    assert g.lut_state()["version"] == 2
    with pytest.raises(clg.LutError) as exc:
        g.grade((0.5, 0.5, 0.5), lut_version=old)
    assert exc.value.code == "LUT_VERSION_MISMATCH"


def test_nan_and_negative_input_are_cleaned():
    g = loaded(axis="r")
    out = g.grade((float("nan"), -1.0, 0.5))
    assert all(math.isfinite(value) and 0.0 <= value <= 1.0
               for value in out)


def test_all_backends_return_the_same_result():
    g = loaded(axis="b")
    d3d = g.grade_backend((0.0, 0.0, 0.25), backend="d3d12")
    vk = g.grade_backend((0.0, 0.0, 0.25), backend="vulkan")
    assert d3d == vk


def test_flipped_lut_matches_flipped_sampling():
    g = loaded(axis="b", flip=True)
    out = g.grade((0.0, 0.0, 0.25))
    assert abs(out[0] - 0.75) < 1e-9


def test_exposure_is_applied_before_lut_sampling():
    dark = loaded(axis="r")
    bright = loaded(axis="r")
    bright.set_exposure(2.0)
    assert dark.grade((0.5, 0.5, 0.5)) == bright.grade((0.25, 0.25, 0.25))


def test_zero_strength_returns_identity_output():
    g = loaded(axis="r", invert=True)
    g.set_strength(0.0)
    out = g.grade((0.25, 0.25, 0.25))
    assert abs(out[0] - 0.25) < 1e-9


def test_output_is_always_finite_and_bounded():
    g = loaded(axis="b")
    for rgb in ((-10.0, 0.0, 3.0), (0.0, 0.0, 0.0), (1e9, 1.0, 0.5)):
        out = g.grade(rgb)
        assert all(math.isfinite(value) and 0.0 <= value <= 1.0
                   for value in out)


def test_reload_is_atomic_for_readers():
    g = loaded(version=1)
    g.reload(lut("b", 2), 2, version=2)
    with pytest.raises(clg.LutError):
        g.grade((0.0, 0.0, 0.25), lut_version=1)
    out = g.grade((0.0, 0.0, 0.25), lut_version=2)
    assert abs(out[0] - 0.25) < 1e-9
