import math

import pytest

import bloom_chain_threshold as bct


def make(**kwargs):
    return bct.BloomChainThreshold(**kwargs)


def test_exposure_adjusted_threshold_keeps_same_highlights():
    b = make(threshold=1.0)
    low = b.extract([3.0, 0.5], exposure=1.0)
    high = b.extract([3.0, 0.5], exposure=2.0)
    assert low[0] > 0.0 and low[1] == 0.0
    assert high[0] > 0.0 and high[1] == 0.0


def test_higher_threshold_produces_less_bloom():
    b = make(threshold=1.0)
    low = b.extract([3.0], exposure=1.0)[0]
    b2 = make(threshold=2.0)
    high = b2.extract([3.0], exposure=1.0)[0]
    assert low > high > 0.0


def test_downsample_filters_before_decimating():
    b = make()
    image = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    out = b.downsample(1, image)
    assert len(out) == 4
    assert all(value > 0.0 for value in out)


def test_composite_normalizes_weights():
    b = make()
    out = b.composite([[2.0, 0.0], [0.0, 2.0]], weights=[1.0, 1.0])
    assert out == [1.0, 1.0]


def test_ui_layer_is_not_bloomed():
    b = make()
    out = b.render([5.0], ui=[0.2], particles=None)
    assert out["ui"] == [0.2]
    assert out["scene"] != [0.2]


def test_disabled_bloom_returns_scene_unchanged():
    b = make()
    b.set_enabled(False)
    out = b.render([0.5, 1.0])
    assert out["scene"] == [0.5, 1.0]


def test_resolution_change_rebuilds_chain_version():
    b = make()
    b.configure(resolution=(64, 64))
    before = b.chain_state()["version"]
    b.set_resolution((32, 32))
    assert b.chain_state()["version"] > before


def test_stale_chain_version_is_rejected():
    b = make()
    b.configure(resolution=(64, 64))
    old = b.chain_state()["version"]
    b.set_resolution((32, 32))
    with pytest.raises(bct.BloomError) as exc:
        b.render([1.0], chain_version=old)
    assert exc.value.code == "STALE_CHAIN"


def test_transparent_particle_blooms_less():
    b = make()
    solid = b.render([0.0], particles=[(10.0, 1.0)])
    faint = b.render([0.0], particles=[(10.0, 0.2)])
    assert solid["scene"][0] > faint["scene"][0] > 0.0


def test_fully_transparent_particle_adds_nothing():
    b = make()
    out = b.render([0.0], particles=[(10.0, 0.0)])
    assert out["scene"] == [0.0]


def test_camera_instances_are_isolated():
    a = make()
    b = make()
    a.set_enabled(False)
    assert a.render([1.0])["scene"] == [1.0]
    assert b.render([1.0])["scene"] != [1.0]


def test_load_scene_clears_residual_levels():
    b = make()
    b.configure(levels=3)
    b.render([4.0])
    b.load_scene("level-2")
    assert b.chain_state()["levels"] == []


def test_render_output_is_finite_and_non_negative():
    b = make()
    out = b.render([-5.0, 0.0, 100.0])
    assert all(math.isfinite(value) and value >= 0.0
               for value in out["scene"])


def test_ui_layer_stays_identical_after_render():
    b = make()
    ui = [0.1, 0.9, 2.0]
    out = b.render([3.0, 0.0, 0.0], ui=list(ui))
    assert out["ui"] == ui


def test_exposure_change_invalidates_old_chain():
    b = make()
    b.configure(exposure=1.0)
    first = b.chain_state()["version"]
    b.set_exposure(2.0)
    second = b.chain_state()["version"]
    assert second > first
    with pytest.raises(bct.BloomError):
        b.render([1.0], chain_version=first)
    assert b.render([1.0], chain_version=second)["scene"]
