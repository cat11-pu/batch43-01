import pytest

import capability_bounding_set


def test_raise_outside_bounding_set_gives_eperm():
    caps = capability_bounding_set.CapabilityBoundingSet()
    caps.drop("net_admin")
    with pytest.raises(capability_bounding_set.CapabilityError) as exc:
        caps.raise_cap("net_admin")
    assert exc.value.code == "EPERM"


def test_drop_removes_capability_from_effective_and_permitted():
    caps = capability_bounding_set.CapabilityBoundingSet()
    caps.drop("net_raw")
    assert "net_raw" not in caps.effective_caps()
    assert "net_raw" not in caps.permitted_caps()


def test_dropped_bounding_capability_cannot_come_back():
    caps = capability_bounding_set.CapabilityBoundingSet()
    caps.drop("net_admin")
    assert "net_admin" not in caps.bounding_caps()
    with pytest.raises(capability_bounding_set.CapabilityError):
        caps.raise_cap("net_admin")
    assert "net_admin" not in caps.bounding_caps()


def test_uid_transition_clears_effective_and_permitted():
    caps = capability_bounding_set.CapabilityBoundingSet()
    result = caps.transition(uid_changed=True, setuid=True)
    assert result["effective"] == set()
    assert result["permitted"] == set()


def test_transition_refusing_to_clear_is_denied():
    caps = capability_bounding_set.CapabilityBoundingSet()
    with pytest.raises(capability_bounding_set.CapabilityError) as exc:
        caps.transition(uid_changed=True, setuid=True, clear_effective=False)
    assert exc.value.code == "CAP_TRANSITION_DENIED"


def test_ambient_capabilities_do_not_survive_setuid_exec():
    caps = capability_bounding_set.CapabilityBoundingSet()
    caps.set_ambient("net_raw")
    result = caps.transition(uid_changed=True, setuid=True)
    assert result["ambient"] == set()


def test_file_caps_conflict_with_no_new_privs():
    caps = capability_bounding_set.CapabilityBoundingSet()
    with pytest.raises(capability_bounding_set.CapabilityError) as exc:
        caps.enable_file_caps(("net_raw",))
    assert exc.value.code == "CAP_NO_NEW_PRIVS_CONFLICT"


def test_uid_change_recomputes_permitted_set():
    caps = capability_bounding_set.CapabilityBoundingSet()
    result = caps.change_uid(1000, allowed_caps=("chown",))
    assert result["permitted"] == {"chown"}
    assert "net_admin" not in caps.permitted_caps()


def test_child_inherits_only_inheritable_capabilities():
    caps = capability_bounding_set.CapabilityBoundingSet()
    caps.set_inheritable("chown")
    child = caps.fork_child()
    assert child["inheritable"] == {"chown"}
    assert child["effective"] == set()
    assert child["permitted"] == set()


def test_child_does_not_inherit_effective_capabilities():
    caps = capability_bounding_set.CapabilityBoundingSet()
    child = caps.fork_child()
    assert "net_admin" not in child["effective"]
    assert child["ambient"] == set()


def test_capability_version_mismatch_is_rejected():
    caps = capability_bounding_set.CapabilityBoundingSet(kernel_version=(5, 15))
    with pytest.raises(capability_bounding_set.CapabilityError) as exc:
        caps.set_version((6, 1))
    assert exc.value.code == "CAP_VERSION_MISMATCH"


def test_drop_unknown_capability_fails_loudly():
    caps = capability_bounding_set.CapabilityBoundingSet()
    with pytest.raises(capability_bounding_set.CapabilityError) as exc:
        caps.drop("teleport")
    assert exc.value.code == "CAP_DROP_FAILED"


def test_ambient_beyond_permitted_is_denied():
    caps = capability_bounding_set.CapabilityBoundingSet()
    caps.drop("net_raw")
    with pytest.raises(capability_bounding_set.CapabilityError) as exc:
        caps.set_ambient("net_raw")
    assert exc.value.code == "CAP_AMBIENT_DENIED"


def test_ambient_raise_does_not_expand_bounding_set():
    caps = capability_bounding_set.CapabilityBoundingSet()
    before = set(caps.bounding_caps())
    caps.set_ambient("net_raw")
    assert caps.bounding_caps() == before


def test_transition_without_uid_change_keeps_capabilities():
    caps = capability_bounding_set.CapabilityBoundingSet()
    before = set(caps.effective_caps())
    result = caps.transition(uid_changed=False, setuid=False)
    assert result["effective"] == before
