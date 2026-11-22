import pytest

import foliage_instancing_meadow as fim


def make(**kwargs):
    return fim.FoliageInstancingMeadow(**kwargs)


def meadow(capacity=100):
    m = make()
    m.add_species("grass", capacity)
    return m


def test_wind_phase_depends_on_world_position_and_time():
    m = meadow()
    a = m.wind_phase(1.0, 2.0, 3.0)
    b = m.wind_phase(1.0, 2.0, 3.0)
    assert a == b
    assert m.wind_phase(1.0, 2.0, 4.0) != a


def test_neighboring_grass_does_not_share_one_phase():
    m = meadow()
    assert m.wind_phase(1.0, 0.0, 0.0) != m.wind_phase(2.0, 0.0, 0.0)


def test_wind_strength_is_frame_rate_independent():
    m = meadow()
    first = m.wind_strength(1.25)
    for _ in range(100):
        m.wind_strength(1.25)
    assert m.wind_strength(1.25) == first


def test_density_falls_off_smoothly_with_distance():
    m = meadow()
    near = m.density_for(49.0)
    far = m.density_for(51.0)
    assert near > far > 0.0
    assert 0.0 < m.density_for(100.0) <= 1.0


def test_root_height_comes_from_terrain_data():
    m = meadow()
    m.set_terrain_height(3.0, 4.0, 12.5, version=1)
    out = m.place("grass", 3.0, 4.0, count=1)
    assert out["placed"] == 1
    assert m.instances("grass")[0]["y"] == 12.5


def test_stale_terrain_version_is_rejected():
    m = meadow()
    m.set_terrain_height(0.0, 0.0, 1.0, version=2)
    with pytest.raises(fim.FoliageError) as exc:
        m.place("grass", 0.0, 0.0, count=1, terrain_version=1)
    assert exc.value.code == "STALE_TERRAIN"
    assert m.instances("grass") == []


def test_species_have_separate_instance_buffers():
    m = meadow()
    m.add_species("reed", 10)
    m.place("grass", 0.0, 0.0, count=2)
    m.place("reed", 1.0, 0.0, count=3)
    assert len(m.instances("grass")) == 2
    assert len(m.instances("reed")) == 3


def test_capacity_overflow_is_rejected_without_wrapping():
    m = meadow(capacity=2)
    m.place("grass", 0.0, 0.0, count=2)
    with pytest.raises(fim.FoliageError) as exc:
        m.place("grass", 1.0, 0.0, count=1)
    assert exc.value.code == "CAPACITY_EXCEEDED"
    assert len(m.instances("grass")) == 2


def test_rebuild_commit_requires_matching_version():
    m = meadow()
    m.place("grass", 0.0, 0.0)
    staged = m.rebuild("grass")
    out = m.commit_rebuild("grass", staged["version"])
    assert out["committed"] is True
    assert m.buffer_version("grass") == staged["version"]


def test_stale_rebuild_cannot_replace_live_buffer():
    m = meadow()
    m.place("grass", 0.0, 0.0)
    m.rebuild("grass")
    old = m.buffer_version("grass")
    with pytest.raises(fim.FoliageError) as exc:
        m.commit_rebuild("grass", old + 99)
    assert exc.value.code == "STALE_REBUILD"
    assert m.buffer_version("grass") == old


def test_shadow_pass_uses_same_lod_as_main_view():
    m = meadow()
    for distance in (5.0, 25.0, 80.0):
        assert m.shadow_lod_for(distance) == m.lod_for(distance)


def test_visible_instance_order_is_stable():
    m = meadow()
    m.place("grass", 1.0, 0.0)
    m.place("grass", 2.0, 0.0)
    first = m.visible_instances("grass", camera_radius=10.0)
    second = m.visible_instances("grass", camera_radius=10.0)
    assert first == second


def test_frustum_radius_culls_far_instances():
    m = meadow()
    m.place("grass", 1.0, 0.0)
    m.place("grass", 100.0, 0.0)
    visible = m.visible_instances("grass", camera_radius=10.0,
                                  origin=(0.0, 0.0))
    assert len(visible) == 1
    assert visible[0]["x"] == 1.0


def test_visible_count_never_exceeds_capacity_after_rebuilds():
    m = meadow(capacity=3)
    m.place("grass", 0.0, 0.0, count=3)
    m.rebuild("grass")
    visible = m.visible_instances("grass", camera_radius=100.0)
    assert len(visible) <= 3


def test_stomped_state_survives_restart():
    m = meadow()
    m.place("grass", 1.0, 1.0)
    m.stomp("grass", 0)
    state = m.dump()
    restored = make()
    restored.load(state)
    assert restored.instances("grass")[0]["stomped"] is True
