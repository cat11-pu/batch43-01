import pytest

import cloth_sim_solver as css


def make(**kwargs):
    return css.ClothSimSolver(**kwargs)


def flat(rows):
    return [value for row in rows for value in row]


def cloth(p, cloth_id="cape", particles=None, anchors=None):
    if particles is None:
        particles = [(0.0, 5.0, 0.0), (0.0, 4.0, 0.0),
                     (0.0, 3.0, 0.0), (0.0, 2.0, 0.0)]
    p.create_cloth(cloth_id, particles, anchors=anchors)
    return cloth_id


def test_frame_chunking_does_not_change_final_shape():
    a = make()
    b = make()
    cloth(a)
    cloth(b)
    a.step("cape", 0.10)
    for _ in range(10):
        b.step("cape", 0.01)
    assert flat(a.positions("cape")) == pytest.approx(
        flat(b.positions("cape")), abs=1e-12)


def test_substeps_and_iterations_do_not_depend_on_frame_rate():
    a = make(iterations=8)
    cloth(a)
    a.step("cape", 0.05)
    assert a.stats("cape")["substeps"] == 5
    assert a.stats("cape")["iterations"] == 8
    b = make(iterations=8)
    cloth(b)
    for _ in range(5):
        b.step("cape", 0.01)
    assert b.stats("cape")["substeps"] == 5
    assert b.stats("cape")["iterations"] == 8


def test_simulation_is_reproducible():
    a = make()
    b = make()
    cloth(a)
    cloth(b)
    for _ in range(3):
        a.step("cape", 0.03)
        b.step("cape", 0.03)
    assert a.positions("cape") == b.positions("cape")


def test_collision_is_resolved_within_the_same_step():
    p = make()
    p.create_cloth("cape", [(0.0, 0.5, 0.0)])
    p.set_collider("body", (0.0, 0.0, 0.0), 1.0)
    p.step("cape", 0.01)
    x, y, z = p.positions("cape")[0]
    assert (x * x + y * y + z * z) ** 0.5 >= 1.0 - 1e-9


def test_moving_collider_pushes_cloth_out_in_the_same_frame():
    p = make(gravity=0.0)
    p.create_cloth("cape", [(0.5, 1.6, 0.0)])
    p.set_collider("platform", (0.0, 0.0, 0.0), 1.0)
    p.step("cape", 0.01)
    p.set_collider("platform", (0.5, 1.0, 0.0), 1.0)
    p.step("cape", 0.01)
    x, y, z = p.positions("cape")[0]
    distance = ((x - 0.5) ** 2 + (y - 1.0) ** 2 + (z - 0.0) ** 2) ** 0.5
    assert distance >= 1.0 - 1e-9


def test_anchor_particle_snaps_to_its_bone():
    p = make()
    p.create_cloth("cape", [(5.0, 0.0, 0.0)], anchors={0: (0.0, 0.0, 0.0)})
    p.step("cape", 0.02)
    assert p.positions("cape")[0] == pytest.approx((0.0, 0.0, 0.0))


def test_anchor_follows_scaled_bone_without_drift():
    p = make()
    p.create_cloth("cape", [(5.0, 0.0, 0.0)], anchors={0: (0.0, 0.0, 0.0)})
    p.set_anchor("cape", 0, (2.0, 0.0, 0.0))
    p.step("cape", 0.02)
    assert p.positions("cape")[0] == pytest.approx((2.0, 0.0, 0.0))


def test_lod_low_keeps_a_subset_of_particles_active():
    p = make()
    cloth(p)
    p.set_lod("cape", "low")
    active = p.active_indices("cape")
    assert active == [0, 2]
    assert set(active) < {0, 1, 2, 3}


def test_switching_back_from_low_lod_is_shape_continuous():
    p = make()
    cloth(p)
    p.step("cape", 0.02)
    before = list(p.positions("cape"))
    p.set_lod("cape", "low")
    p.set_lod("cape", "high")
    after = p.positions("cape")
    for index in (0, 1, 2, 3):
        assert after[index] == pytest.approx(before[index])


def test_restored_snapshot_reproduces_next_step():
    p = make()
    cloth(p)
    p.step("cape", 0.02)
    state = p.dump()
    q = make()
    q.load(state)
    p.step("cape", 0.03)
    q.step("cape", 0.03)
    assert flat(p.positions("cape")) == pytest.approx(
        flat(q.positions("cape")), abs=1e-12)


def test_wind_is_transformed_into_character_space():
    p = make()
    cloth(p)
    p.set_wind("cape", (1.0, 0.0, 0.0))
    assert p.wind_local("cape") == pytest.approx((1.0, 0.0, 0.0))
    p.set_character_rotation("cape", 90)
    assert p.wind_local("cape") == pytest.approx((0.0, 0.0, 1.0))


def test_zero_gravity_leaves_free_particles_still():
    p = make(gravity=0.0)
    p.create_cloth("cape", [(1.0, 2.0, 3.0)])
    p.step("cape", 0.05)
    assert p.positions("cape")[0] == pytest.approx((1.0, 2.0, 3.0))


def test_zero_frame_dt_does_not_advance_the_simulation():
    p = make()
    cloth(p)
    p.step("cape", 0.02)
    before = p.stats("cape")["substeps"]
    p.step("cape", 0.0)
    assert p.stats("cape")["substeps"] == before


def test_reset_restores_initial_positions():
    p = make()
    cloth(p)
    p.step("cape", 0.2)
    p.reset("cape")
    assert flat(p.positions("cape")) == pytest.approx(
        [0.0, 5.0, 0.0, 0.0, 4.0, 0.0,
         0.0, 3.0, 0.0, 0.0, 2.0, 0.0])


def test_device_restore_keeps_cloth_geometry():
    p = make()
    cloth(p)
    p.step("cape", 0.05)
    before = list(p.positions("cape"))
    p.device_lost()
    p.device_restored()
    assert flat(p.positions("cape")) == pytest.approx(flat(before))
