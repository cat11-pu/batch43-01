import pytest

import taa_motion_reprojector as tmr


def make(**kwargs):
    return tmr.TaaMotionReprojector(**kwargs)


def frame(t, camera="cam1", resolution=(100, 100), jitter=(0.0, 0.0)):
    t.create_view("main")
    return t.begin_frame(camera, [[1.0, 0.0], [0.0, 1.0]],
                         jitter=jitter, resolution=resolution)


def test_motion_vector_is_in_screen_space_pixels():
    t = make()
    frame(t, resolution=(100, 100))
    out = t.object_motion("hero", (0.0, 0.0), (0.5, 0.0))
    assert out["motion"] == (25.0, 0.0)


def test_jitter_is_part_of_the_frame_snapshot():
    t = make()
    frame(t, jitter=(0.5, -0.25))
    state = t.frame_state()
    assert state["jitter"] == (0.5, -0.25)
    assert state["camera_id"] == "cam1"


def test_camera_cut_invalidates_history():
    t = make()
    frame(t)
    t.commit(1)
    assert t.history_valid() is True
    t.begin_frame("cam2", [[1.0, 0.0], [0.0, 1.0]], resolution=(100, 100))
    assert t.history_valid() is False
    with pytest.raises(tmr.TaaError) as exc:
        t.reproject(1, (0.0, 0.0))
    assert exc.value.code == "HISTORY_INVALIDATED"


def test_teleport_invalidates_object_history():
    t = make()
    frame(t)
    t.commit(1)
    out = t.object_motion("hero", (0.0, 0.0), (0.5, 0.5), teleport=True)
    assert out["teleport"] is True
    assert t.history_valid() is False


def test_resolution_change_invalidates_history():
    t = make()
    frame(t)
    t.commit(1)
    t.set_resolution((50, 50))
    assert t.history_valid() is False


def test_particles_are_not_accumulated():
    t = make()
    assert t.should_accumulate("particle") is False


def test_transparent_layers_are_not_accumulated():
    t = make()
    assert t.should_accumulate("transparent") is False


def test_ui_layers_are_not_accumulated():
    t = make()
    assert t.should_accumulate("ui") is False
    assert t.should_accumulate("opaque") is True


def test_reproject_coordinates_are_resolution_independent():
    t = make()
    low = t.reproject(1, (0.25, 0.1), resolution=(32, 32))
    high = t.reproject(1, (0.25, 0.1), resolution=(128, 128))
    assert low["uv"] == high["uv"]


def test_split_screen_views_keep_independent_history():
    t = make()
    t.create_view("left")
    t.create_view("right")
    t.invalidate_history("cut", view_id="left")
    assert t.history_valid("left") is False
    assert t.history_valid("right") is True


def test_blend_factor_is_capped():
    t = make(blend=0.2, max_history=0.6)
    frame(t)
    first = t.frame_state()["blend"]
    t.begin_frame("cam1", [[1.0, 0.0], [0.0, 1.0]], resolution=(100, 100))
    second = t.frame_state()["blend"]
    assert first <= second <= 0.6


def test_commit_with_wrong_frame_version_is_rejected():
    t = make()
    frame(t)
    with pytest.raises(tmr.TaaError) as exc:
        t.commit(99)
    assert exc.value.code == "FRAME_MISMATCH"


def test_motion_and_camera_must_come_from_same_snapshot():
    t = make()
    frame(t)
    t.set_resolution((50, 50))
    with pytest.raises(tmr.TaaError) as exc:
        t.object_motion("hero", (0.0, 0.0), (0.1, 0.0))
    assert exc.value.code == "CAMERA_SNAPSHOT_MISMATCH"


def test_stale_history_version_cannot_be_committed():
    t = make()
    frame(t)
    t.commit(1)
    t.begin_frame("cam1", [[1.0, 0.0], [0.0, 1.0]], resolution=(100, 100))
    with pytest.raises(tmr.TaaError) as exc:
        t.commit(1)
    assert exc.value.code == "FRAME_MISMATCH"


def test_motion_vector_is_clamped_to_screen_extent():
    t = make()
    frame(t, resolution=(100, 100))
    out = t.object_motion("hero", (0.0, 0.0), (100.0, -100.0))
    dx, dy = out["motion"]
    assert -50.0 <= dx <= 50.0
    assert -50.0 <= dy <= 50.0
