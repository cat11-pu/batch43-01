import pytest

import terrain_lod_stitcher as tls


def make(**kwargs):
    kwargs.setdefault("chunk_size", 4)
    return tls.TerrainLodStitcher(**kwargs)


def heightmap(size=10, value=None):
    return [[float(value if value is not None else x + y)
             for x in range(size)] for y in range(size)]


def ready(size=10, value=None, version=1):
    t = make()
    t.set_heightmap(heightmap(size, value), version=version)
    return t


def test_adjacent_same_lod_borders_match_without_crack():
    t = ready()
    a = t.build_chunk(0, 0, lod=0)
    b = t.build_chunk(1, 0, lod=0)
    out = t.stitch(a["chunk_id"], b["chunk_id"])
    assert out["crack"] is False
    left = t.border_heights(a["chunk_id"])
    right = t.border_heights(b["chunk_id"])
    assert left[(4, 2)] == right[(4, 2)]


def test_adjacent_different_lod_borders_still_match():
    t = ready()
    a = t.build_chunk(0, 0, lod=0)
    b = t.build_chunk(1, 0, lod=2)
    out = t.stitch(a["chunk_id"], b["chunk_id"])
    assert out["crack"] is False


def test_physics_and_render_height_agree():
    t = ready(value=3)
    assert t.render_height(2, 3) == t.physics_height(2, 3)
    assert abs(t.render_height(2, 3) - 3.0) <= t.height_epsilon
    assert t.normal_at(2, 3) == t.normal_at(2, 3)


def test_morph_factor_follows_split_distance():
    t = make(split_distance=32.0)
    assert t.morph_factor(0.0) == 0.0
    assert t.morph_factor(16.0) == 0.5
    assert t.morph_factor(32.0) == 1.0
    assert t.morph_factor(64.0) == 1.0


def test_stale_rebuild_result_is_discarded():
    t = ready(version=2)
    chunk = t.build_chunk(0, 0, lod=0)
    out = t.apply_rebuild(chunk["chunk_id"], result_version=1)
    assert out["applied"] is False
    assert out["stale"] is True
    assert t.chunk_state(chunk["chunk_id"])["version"] == 2


def test_hole_mask_survives_lod_changes():
    t = ready(value=1)
    t.set_hole_mask([[False] * 10 for _ in range(10)])
    assert t.is_hole(1, 1) is False
    t.build_chunk(0, 0, lod=3)
    assert t.is_hole(1, 1) is False


def test_visible_chunks_include_seam_neighbors():
    t = ready()
    t.build_chunk(0, 0)
    visible = t.visible_chunks("0:0")
    assert "0:0" in visible
    assert "1:0" in visible
    assert "0:1" in visible
    assert len(visible) == 9


def test_index_width_switches_for_large_meshes():
    t = ready()
    assert t.index_type(1000) == "uint16"
    assert t.index_type(70000) == "uint32"


def test_skirt_vertices_sit_below_border():
    t = ready(value=5)
    chunk = t.build_chunk(0, 0, lod=0)
    skirts = t.skirt_vertices(chunk["chunk_id"])
    assert len(skirts) > 0
    for vertex in skirts:
        assert vertex["height"] < t.render_height(vertex["x"], vertex["y"])


def test_mixed_heightmap_versions_cannot_be_stitched():
    t = ready(version=1)
    a = t.build_chunk(0, 0, lod=0)
    t.set_heightmap(heightmap(value=2), version=2)
    b = t.build_chunk(1, 0, lod=0)
    with pytest.raises(tls.TerrainError) as exc:
        t.stitch(a["chunk_id"], b["chunk_id"])
    assert exc.value.code == "STALE_CHUNK"


def test_large_height_values_keep_their_value():
    t = ready(size=10, value=1000000)
    assert t.render_height(0, 0) == 1000000.0
    assert t.physics_height(9, 9) == 1000000.0


def test_borders_do_not_depend_on_lod():
    t = ready()
    low = t.build_chunk(0, 0, lod=0)
    high = t.build_chunk(0, 0, lod=3)
    assert t.border_heights(low["chunk_id"]) == t.border_heights(
        high["chunk_id"])


def test_no_crack_for_any_view_position():
    t = ready()
    for view in ((0, 0), (1, 1), (2, 3)):
        t.set_view(*view)
        left = t.build_chunk(0, 0, lod=0)
        right = t.build_chunk(1, 0, lod=1)
        assert t.stitch(left["chunk_id"], right["chunk_id"])["crack"] is False


def test_physics_height_error_stays_within_threshold():
    t = ready()
    for x in range(5):
        for y in range(5):
            delta = abs(t.render_height(x, y) - t.physics_height(x, y))
            assert delta <= t.height_epsilon


def test_stale_async_result_does_not_swap_mesh():
    t = ready(version=3)
    chunk = t.build_chunk(0, 0, lod=1)
    t.apply_rebuild(chunk["chunk_id"], result_version=2)
    state = t.chunk_state(chunk["chunk_id"])
    assert state["version"] == 3
    assert state["lod"] == 1
