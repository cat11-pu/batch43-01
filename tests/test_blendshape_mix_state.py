import math

import pytest

import blendshape_mix_state as bms


def channels():
    return {
        "smile": {"position": (1.0, 0.0, 0.0), "normal": (1.0, 0.0, 0.0)},
        "frown": {"position": (0.0, 1.0, 0.0), "normal": (0.0, 1.0, 0.0)},
        "eye_blink": {"position": (0.0, 0.0, 1.0), "normal": (0.0, 0.0, 1.0)},
        "mouth_open": {"position": (0.0, 0.0, 2.0), "normal": (0.0, 0.0, 1.0)},
    }


def make(**kwargs):
    kwargs.setdefault("channels", channels())
    kwargs.setdefault("lod_map", {"eye_blink": "eye_blink_low"})
    return bms.BlendshapeMixState(**kwargs)


def applied(p, character="hero", weights=None, frame=0):
    p.mix(character, weights, frame=frame)
    return p.apply(character, frame=frame)


def test_weights_sum_above_one_raise_value_error():
    p = make()
    with pytest.raises(ValueError) as exc:
        p.mix("hero", {"smile": 0.7, "frown": 0.6}, frame=1)
    assert getattr(exc.value, "code", "") == "weights_exceed_one"
    assert p.weights("hero") == {}


def test_negative_weight_raises_value_error():
    p = make()
    with pytest.raises(ValueError) as exc:
        p.mix("hero", {"smile": -0.1}, frame=1)
    assert getattr(exc.value, "code", "") == "negative_weight"


def test_blend_uses_base_for_the_remaining_weight():
    p = make()
    out = applied(p, weights={"smile": 0.5})
    assert out["position"] == pytest.approx((0.5, 0.0, 0.0))


def test_weight_returning_to_zero_leaves_no_residual():
    p = make()
    applied(p, weights={"smile": 1.0}, frame=1)
    out = applied(p, weights={"smile": 0.0}, frame=2)
    assert out["position"] == pytest.approx((0.0, 0.0, 0.0))


def test_blended_normal_stays_normalized():
    p = make()
    out = applied(p, weights={"smile": 0.5, "frown": 0.5})
    nx, ny, nz = out["normal"]
    assert math.sqrt(nx * nx + ny * ny + nz * nz) == pytest.approx(1.0)
    assert nx == pytest.approx(ny)


def test_animation_event_reads_final_weights_only_after_apply():
    p = make()
    p.mix("hero", {"smile": 0.4}, frame=1)
    p.apply("hero", frame=1)
    p.mix("hero", {"smile": 0.9}, frame=2)
    early = p.emit_event("hero", "say", frame=2)
    assert early["weights"] == {"smile": 0.4}
    p.apply("hero", frame=2)
    late = p.emit_event("hero", "say", frame=3)
    assert late["weights"] == {"smile": 0.9}


def test_lod_switch_preserves_weights_by_channel_mapping():
    p = make()
    p.mix("hero", {"eye_blink": 0.6}, frame=1)
    p.apply("hero", frame=1)
    p.set_lod("hero", "low")
    assert p.weights("hero") == {"eye_blink_low": 0.6}
    p.set_lod("hero", "high")
    assert p.weights("hero") == {"eye_blink": 0.6}


def test_unknown_channel_raises_instead_of_truncating():
    p = make()
    with pytest.raises(bms.BlendshapeError) as exc:
        p.set("hero", "nonexistent", 0.5, frame=1)
    assert exc.value.code == "unknown_channel"
    assert p.weights("hero") == {}


def test_stale_frame_cannot_overwrite_newer_state():
    p = make()
    applied(p, weights={"smile": 0.8}, frame=5)
    stale = p.mix("hero", {"smile": 0.2}, frame=4)
    assert stale["status"] == "stale_frame"
    out = p.apply("hero", frame=4)
    assert out["status"] == "stale_frame"
    assert p.weights("hero") == {"smile": 0.8}


def test_two_characters_do_not_share_blendshape_state():
    p = make()
    applied(p, "hero", {"smile": 0.5}, frame=1)
    applied(p, "npc", {"frown": 0.25}, frame=1)
    assert p.weights("hero") == {"smile": 0.5}
    assert p.weights("npc") == {"frown": 0.25}


def test_reset_returns_to_base_pose():
    p = make()
    applied(p, weights={"smile": 0.7}, frame=1)
    p.reset("hero")
    assert p.weights("hero") == {}
    out = p.apply("hero", frame=2)
    assert out["position"] == pytest.approx((0.0, 0.0, 0.0))


def test_save_and_load_preserve_weight_precision():
    p = make()
    p.mix("hero", {"smile": 0.125, "frown": 0.375}, frame=1)
    p.apply("hero", frame=1)
    state = p.dump()
    q = make()
    q.load(state)
    assert q.weights("hero") == {"smile": 0.125, "frown": 0.375}


def test_reloaded_channels_invalidate_old_handles():
    p = make()
    p.set("hero", "smile", 0.5, frame=1)
    p.reload_channels({"new_channel": {"position": (1, 1, 1),
                                       "normal": (0, 0, 1)}})
    with pytest.raises(bms.BlendshapeError) as exc:
        p.set("hero", "smile", 0.5, frame=2)
    assert exc.value.code == "unknown_channel"
    p.set("hero", "new_channel", 0.5, frame=3)


def test_device_restore_keeps_current_weights():
    p = make()
    p.mix("hero", {"smile": 0.25}, frame=1)
    p.apply("hero", frame=1)
    p.device_lost()
    p.device_restored()
    assert p.weights("hero") == {"smile": 0.25}
    out = p.apply("hero", frame=2)
    assert out["position"] == pytest.approx((0.25, 0.0, 0.0))


def test_blink_and_talk_channels_are_independent():
    p = make()
    out = applied(p, weights={"eye_blink": 0.3, "mouth_open": 0.3})
    assert out["weights"] == {"eye_blink": 0.3, "mouth_open": 0.3}
    assert out["position"] == pytest.approx((0.0, 0.0, 0.9))
