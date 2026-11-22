import pytest

import staging_ring_alloc as sra


def make(**kwargs):
    return sra.StagingRingAlloc(**kwargs)


def test_buffer_offsets_respect_four_byte_alignment():
    p = make()
    first = p.alloc(3, kind="buffer")
    second = p.alloc(5, kind="buffer")
    assert first["offset"] == 0
    assert second["offset"] % 4 == 0
    assert second["offset"] >= 4


def test_constant_offsets_respect_two_hundred_fifty_six_bytes():
    p = make(capacity=1024)
    first = p.alloc(1, kind="constant")
    second = p.alloc(1, kind="constant")
    assert first["offset"] == 0
    assert second["offset"] == 256


def test_alignment_below_kind_minimum_is_rejected():
    p = make()
    with pytest.raises(sra.MisalignedUpload):
        p.alloc(16, kind="constant", alignment=4)
    with pytest.raises(sra.MisalignedUpload):
        p.alloc(16, kind="texture", alignment=8)


def test_exhausted_ring_raises_instead_of_overwriting():
    p = make(capacity=64)
    first = p.alloc(32)
    p.write(first, b"a" * 32)
    second = p.alloc(32)
    p.write(second, b"b" * 32)
    with pytest.raises(sra.RingExhausted):
        p.alloc(8)


def test_existing_data_survives_an_exhaustion_error():
    p = make(capacity=64)
    first = p.alloc(32)
    p.write(first, b"x" * 32)
    p.alloc(32)
    with pytest.raises(sra.RingExhausted):
        p.alloc(8, wait=True)
    assert p.read(first) == b"x" * 32
    assert p.state()["free_bytes"] == 0


def test_complete_and_recycle_make_room_again():
    p = make(capacity=64)
    first = p.alloc(32, frame=0)
    p.write(first, b"y" * 32)
    p.alloc(32, frame=1)
    with pytest.raises(sra.RingExhausted):
        p.alloc(8)
    p.complete(first)
    assert p.recycle(0)["freed"] == 32
    assert p.alloc(16)["offset"] % 4 == 0


def test_recycle_before_completion_is_rejected():
    p = make(capacity=64)
    p.alloc(16)
    with pytest.raises(sra.StagingError) as exc:
        p.recycle(0)
    assert exc.value.code == "fence_pending"


def test_waiting_alloc_still_fails_explicitly_when_full():
    p = make(capacity=32)
    first = p.alloc(32)
    p.write(first, b"z" * 32)
    with pytest.raises(sra.RingExhausted):
        p.alloc(1, wait=True)
    assert p.read(first) == b"z" * 32


def test_concurrent_allocations_get_disjoint_ranges():
    p = make(capacity=128)
    first = p.alloc(16)
    second = p.alloc(16)
    assert first["offset"] != second["offset"]
    first_end = first["offset"] + first["size"]
    second_end = second["offset"] + second["size"]
    assert first_end <= second["offset"] or second_end <= first["offset"]


def test_marker_is_bound_to_its_frame():
    p = make(capacity=128)
    first = p.alloc(16, frame=0)
    second = p.alloc(16, frame=1)
    p.write(first, b"q" * 16)
    p.complete(first)
    assert p.recycle(0)["freed"] == 16
    with pytest.raises(sra.StagingError) as exc:
        p.recycle(1)
    assert exc.value.code == "fence_pending"


def test_partial_writes_bump_version_only_when_coverage_completes():
    p = make(capacity=64)
    marker = p.alloc(8)
    first = p.write(marker, b"abcd", sub_offset=0, partial=True)
    assert first["version"] == 0
    second = p.write(marker, b"efgh", sub_offset=4, partial=True)
    assert second["version"] == 1
    other = p.alloc(8)
    full = p.write(other, b"12345678")
    assert full["version"] == 1


def test_completion_requires_the_whole_range():
    p = make(capacity=64)
    marker = p.alloc(8)
    p.write(marker, b"abcd", sub_offset=0, partial=True)
    with pytest.raises(sra.StagingError) as exc:
        p.complete(marker)
    assert exc.value.code == "incomplete_upload"


def test_state_reports_correct_free_bytes():
    p = make(capacity=64)
    p.alloc(16)
    p.alloc(16)
    assert p.state()["free_bytes"] == 32
    assert p.state()["used_bytes"] == 32


def test_wrap_requires_a_barrier_on_flush():
    p = make(capacity=64)
    first = p.alloc(48, frame=0)
    p.write(first, b"a" * 48)
    p.complete(first)
    p.recycle(0)
    second = p.alloc(48, frame=1)
    out = p.flush(1)
    assert second["offset"] == 0
    assert out["barrier"] is True


def test_device_restore_bumps_generation_and_rejects_old_markers():
    p = make(capacity=64)
    marker = p.alloc(16)
    p.device_lost()
    p.device_restored()
    with pytest.raises(sra.StagingError) as exc:
        p.complete(marker)
    assert exc.value.code == "stale_marker"
    assert p.state()["generation"] == 2
    assert p.state()["free_bytes"] == 64
    assert p.alloc(16)["offset"] == 0
