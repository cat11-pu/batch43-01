import pytest

import ssao_kernel_denoiser as skd


def make(**kwargs):
    return skd.SsaoKernelDenoiser(**kwargs)


def flat(value, size=3):
    return [[value for _ in range(size)] for _ in range(size)]


def normals(size=3):
    return [[(0.0, 0.0, 1.0) for _ in range(size)] for _ in range(size)]


def ready(depth, size=3, version=1, **kwargs):
    d = make(**kwargs)
    d.set_camera(resolution=(size, size))
    d.set_depth(depth, version=version)
    d.set_normals(normals(size), version=version)
    d.swap_buffers(version)
    return d


def test_sky_pixels_have_zero_occlusion():
    d = ready(flat(1.0))
    d.set_sky([[False, False, False], [False, True, False],
               [False, False, False]])
    occ = d.generate()["occlusion"]
    assert occ[1][1] == 0.0


def test_flat_plane_has_no_false_occlusion():
    d = ready(flat(2.0))
    occ = d.generate()["occlusion"]
    assert all(value == 0.0 for row in occ for value in row)


def test_closer_neighbor_occludes_center():
    depth = flat(1.0)
    depth[0][1] = 0.5
    d = ready(depth)
    occ = d.generate()["occlusion"]
    assert occ[1][1] > 0.0
    assert occ[1][0] == 0.0


def test_far_occluder_is_weaker_than_near_occluder():
    near_depth = flat(1.0)
    near_depth[0][1] = 0.5
    far_depth = flat(10.0)
    far_depth[0][1] = 9.5
    near = ready(near_depth).generate()["occlusion"][1][1]
    far = ready(far_depth).generate()["occlusion"][1][1]
    assert near > far > 0.0


def test_occlusion_is_clamped_to_unit_range():
    depth = flat(1.0)
    depth[0][1] = 0.1
    depth[1][0] = 0.1
    depth[1][2] = 0.1
    depth[2][1] = 0.1
    d = ready(depth, strength=4.0)
    occ = d.generate()["occlusion"]
    assert all(0.0 <= value <= 1.0 for row in occ for value in row)


def test_shading_is_multiplicative_and_clamped():
    d = make(strength=1.0, min_ambient=0.3)
    assert abs(d.shade(0.5) - 0.5) < 1e-9
    assert d.shade(2.0) == 0.3


def test_zero_intensity_keeps_full_light():
    d = make()
    assert d.shade(0.9, intensity=0.0) == 1.0


def test_transparent_cells_do_not_occlude():
    depth = flat(1.0)
    depth[0][1] = 0.5
    d = ready(depth)
    d.set_transparent([[False, True, False], [False, False, False],
                       [False, False, False]])
    occ = d.generate()["occlusion"]
    assert occ[1][1] == 0.0


def test_resolution_change_keeps_kernel_unchanged():
    d = make()
    d.set_camera(resolution=(3, 3))
    before = d.sample_kernel()
    d.set_resolution((6, 6))
    after = d.sample_kernel()
    assert before == after


def test_camera_fov_change_rebuilds_kernel():
    d = make()
    d.set_camera(fov=1.0, resolution=(3, 3))
    before = d.sample_kernel()
    d.set_camera(fov=2.0, resolution=(3, 3))
    after = d.sample_kernel()
    assert before != after


def test_missing_normal_buffer_is_not_renderable():
    d = make()
    d.set_camera(resolution=(3, 3))
    d.set_depth(flat(1.0), version=1)
    d.swap_buffers(1)
    with pytest.raises(skd.SsaoError) as exc:
        d.generate()
    assert exc.value.code == "BUFFER_NOT_READY"


def test_stale_frame_version_is_rejected():
    d = ready(flat(1.0), version=2)
    with pytest.raises(skd.SsaoError) as exc:
        d.generate(frame_version=1)
    assert exc.value.code == "STALE_BUFFER"


def test_swap_buffers_publishes_the_new_snapshot():
    d = make()
    d.set_camera(resolution=(3, 3))
    d.set_depth(flat(1.0), version=1)
    d.set_normals(normals(3), version=1)
    with pytest.raises(skd.SsaoError):
        d.generate()
    d.swap_buffers(1)
    out = d.generate()
    assert out["version"] == 1


def test_near_plane_pixel_still_receives_occlusion():
    near_depth = flat(0.02)
    near_depth[0][1] = 0.01
    d = make()
    d.set_camera(near=0.01, far=100.0, resolution=(3, 3))
    d.set_depth(near_depth, version=1)
    d.set_normals(normals(3), version=1)
    d.swap_buffers(1)
    occ = d.generate()["occlusion"]
    assert occ[1][1] > 0.0


def test_occlusion_grid_matches_resolution_and_ambient_floor():
    d = ready(flat(1.0), size=4)
    out = d.generate()
    assert len(out["occlusion"]) == 4
    assert all(len(row) == 4 for row in out["occlusion"])
    for row in out["occlusion"]:
        for value in row:
            assert 0.0 <= d.shade(value) <= 1.0
