import pytest

import water_surface_compositor as wsc


def make(quality="high", **kwargs):
    return wsc.WaterSurfaceCompositor(quality, **kwargs)


def test_physics_and_render_read_the_same_height():
    p = make()
    for x, y, t in ((0.0, 0.0, 0.0), (1.3, -2.4, 5.0), (-7.0, 3.1, 11.5)):
        assert p.wave_height(x, y, t) == pytest.approx(
            p.render_height(x, y, t), abs=1e-12)


def test_wave_height_is_deterministic():
    p = make()
    first = p.wave_height(2.5, -1.5, 4.0)
    second = p.wave_height(2.5, -1.5, 4.0)
    assert first == second


def test_quality_switch_updates_physics_and_render_amplitude():
    p = make("high")
    high = p.wave_height(1.0, 2.0, 0.0)
    p.set_quality("low")
    low = p.wave_height(1.0, 2.0, 0.0)
    assert low == pytest.approx(high * 0.5)
    assert p.render_height(1.0, 2.0, 0.0) == pytest.approx(low)


def test_quality_switch_rebuilds_mesh_resolution():
    p = make("high")
    assert p.mesh_resolution() == 64
    p.set_quality("low")
    assert p.mesh_resolution() == 32
    p.set_quality("high")
    assert p.mesh_resolution() == 64


def test_adjacent_lods_share_exact_edge_vertices():
    p = make()
    high = set(p.edge_vertices("high"))
    low = set(p.edge_vertices("low"))
    shared = p.seam("high", "low")
    assert set(shared) == low
    assert set(shared) <= high


def test_seam_has_no_duplicate_or_missing_vertices():
    p = make()
    shared = p.seam("high", "low")
    assert len(shared) == len(set(shared))
    high = p.edge_vertices("high")
    assert all(shared[i] < shared[i + 1] for i in range(len(shared) - 1))
    assert shared[0] == high[0] and shared[-1] == high[-1]


def test_depth_color_ramps_smoothly_between_shallow_and_deep():
    p = make()
    shallow = p.blend_color(0.0)
    deep = p.blend_color(20.0)
    middle = p.blend_color(5.0)
    assert shallow == pytest.approx((0.2, 0.6, 0.6))
    assert deep == pytest.approx((0.0, 0.1, 0.4))
    for index in range(3):
        assert deep[index] <= middle[index] <= shallow[index]
        assert middle[index] not in (deep[index], shallow[index])


def test_foam_width_shrinks_on_steep_slope_and_follows_wave():
    p = make()
    flat = p.foam(1.0, 2.0, 0.0, slope=0.0)
    steep = p.foam(1.0, 2.0, 0.0, slope=3.0)
    assert flat["width"] == pytest.approx(1.0)
    assert steep["width"] == pytest.approx(0.25)
    assert flat["height"] == pytest.approx(p.wave_height(1.0, 2.0, 0.0))


def test_refraction_is_clipped_for_geometry_above_the_surface():
    p = make()
    assert p.refract(surface_depth=5.0, scene_depth=2.0)["clipped"] is True
    assert p.refract(surface_depth=5.0, scene_depth=9.0)["clipped"] is False


def test_transparency_sort_is_stable_across_frames():
    p = make()
    items = [
        {"id": "water", "opaque": False, "distance": 10.0},
        {"id": "sail", "opaque": False, "distance": 5.0},
        {"id": "rock", "opaque": True, "distance": 3.0},
    ]
    assert p.sort_order(items) == p.sort_order(list(items))


def test_transparency_order_is_opaque_first_then_far_to_near():
    p = make()
    items = [
        {"id": "water", "opaque": False, "distance": 10.0},
        {"id": "sail", "opaque": False, "distance": 5.0},
        {"id": "rock", "opaque": True, "distance": 3.0},
    ]
    assert p.sort_order(items) == ["rock", "water", "sail"]


def test_calm_water_specular_does_not_jitter():
    p = make()
    first = p.specular(1.0, 1.0, 0.0)
    second = p.specular(1.0, 1.0, 0.01)
    assert abs(second - first) < 0.05
    assert p.specular(1.0, 1.0, 0.0) == pytest.approx(first)


def test_wakes_expire_and_do_not_leak():
    p = make()
    for index in range(100):
        p.add_wake("w%d" % index, 0.0, 0.0, t=0.0, ttl=10.0)
    assert len(p.active_wakes(5.0)) == 100
    assert p.active_wakes(11.0) == []


def test_camera_near_or_under_water_still_renders_surface():
    p = make()
    surface = p.wave_height(0.0, 0.0, 0.0)
    above = p.surface_mode(surface + 1.0, 0.0, 0.0, 0.0)
    below = p.surface_mode(surface - 1.0, 0.0, 0.0, 0.0)
    assert above == "above"
    assert below == "underwater"
    assert p.visible(surface + 0.01, near=0.1) is True
    assert p.visible(surface - 0.01, near=0.1) is True


def test_device_restore_rebuilds_water_shader():
    p = make()
    p.device_lost()
    p.device_restored()
    assert p.shader_state() == "ok"
