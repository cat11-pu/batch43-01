import pytest

import mpls_label_allocator


def test_reserved_label_zero_is_rejected():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    with pytest.raises(mpls_label_allocator.MplsError) as exc:
        allocator.assign_label(0, "fec-a")
    assert exc.value.code == "MPLS_LABEL_RESERVED"


def test_reserved_label_fifteen_is_rejected():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    with pytest.raises(mpls_label_allocator.MplsError) as exc:
        allocator.assign_label(15, "fec-a")
    assert exc.value.code == "MPLS_LABEL_RESERVED"


def test_same_fec_gets_one_label():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    first = allocator.allocate("fec-a")
    second = allocator.allocate("fec-a")
    assert second["label"] == first["label"]
    assert second["reused"] is True


def test_distinct_fecs_get_distinct_labels():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    first = allocator.allocate("fec-a")
    second = allocator.allocate("fec-b")
    assert first["label"] != second["label"]


def test_released_label_returns_to_pool():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    released = allocator.allocate("fec-a")["label"]
    assert allocator.release_fec("fec-a")["released"] is True
    assert allocator.allocate("fec-b")["label"] == released


def test_restart_keeps_in_use_labels_reserved():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    old_label = allocator.allocate("fec-a")["label"]
    state = allocator.dump_state()
    restarted = mpls_label_allocator.MplsLabelAllocator()
    restarted.load_state(state)
    assert restarted.allocate("fec-a")["label"] == old_label
    assert restarted.allocate("fec-b")["label"] != old_label


def test_stack_deeper_than_limit_is_rejected():
    allocator = mpls_label_allocator.MplsLabelAllocator(stack_limit=2)
    with pytest.raises(mpls_label_allocator.MplsError) as exc:
        allocator.build_label_stack("fec-a", ["100", "200"])
    assert exc.value.code == "MPLS_STACK_EXCEEDED"


def test_stack_at_limit_is_accepted():
    allocator = mpls_label_allocator.MplsLabelAllocator(stack_limit=2)
    stack = allocator.build_label_stack("fec-a", ["100"])
    assert stack["depth"] == 2
    assert len(stack["labels"]) == 2


def test_pop_decrements_ttl():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    result = allocator.pop(["16", "100"], ttl=64)
    assert result["ttl"] == 63
    assert result["labels"] == ["100"]


def test_pop_preserves_experimental_bits():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    result = allocator.pop(["16"], ttl=10, experimental_bits=3)
    assert result["experimental_bits"] == 3


def test_installed_mapping_resolves_next_hop():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    allocator.install_mapping(16, "10.0.0.2")
    allocator.install_mapping(17, "10.0.0.3")
    assert allocator.lookup_next_hop(16) == "10.0.0.2"
    assert allocator.lookup_next_hop(17) == "10.0.0.3"
    assert allocator.lookup_next_hop(18) is None


def test_same_label_for_two_next_hops_is_rejected():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    allocator.install_mapping(16, "10.0.0.2")
    with pytest.raises(mpls_label_allocator.MplsError) as exc:
        allocator.install_mapping(16, "10.0.0.9")
    assert exc.value.code == "MPLS_LABEL_CONFLICT"
    assert allocator.lookup_next_hop(16) == "10.0.0.2"


def test_backup_switch_without_ready_backup_keeps_primary():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    with pytest.raises(mpls_label_allocator.MplsError) as exc:
        allocator.switch_to_backup("fec-a", backup_ready=False)
    assert exc.value.code == "MPLS_BACKUP_NOT_READY"
    assert allocator.active_path("fec-a") == "primary"


def test_backup_switch_after_install_moves_traffic():
    allocator = mpls_label_allocator.MplsLabelAllocator()
    result = allocator.switch_to_backup("fec-a", backup_ready=True)
    assert result["switched"] is True
    assert result["path"] == "backup"
    assert allocator.active_path("fec-a") == "backup"


def test_label_pool_exhaustion_is_reported():
    allocator = mpls_label_allocator.MplsLabelAllocator(label_min=16, label_max=16)
    assert allocator.allocate("fec-a")["label"] == 16
    with pytest.raises(mpls_label_allocator.MplsError) as exc:
        allocator.allocate("fec-b")
    assert exc.value.code == "MPLS_LABEL_EXHAUSTED"
