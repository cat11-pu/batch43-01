import vlan_boundary_enforcer


def _enforcer(state_path=None, **kwargs):
    return vlan_boundary_enforcer.VlanBoundaryEnforcer(state_path=state_path, **kwargs)


def _access(enforcer, port, vlan=100, **kwargs):
    enforcer.configure_access(port, vlan, native_vlan=vlan, **kwargs)


def test_double_tagged_frame_dropped():
    enforcer = _enforcer()
    _access(enforcer, 1)
    result = enforcer.frame("aa", 100, 1, tags=(100, 200))
    assert result["action"] == "drop"
    assert result["code"] == "VLAN_DOUBLE_TAG"


def test_tagged_frame_with_wrong_native_vlan_dropped():
    enforcer = _enforcer()
    _access(enforcer, 1, vlan=100)
    result = enforcer.frame("aa", 200, 1, tags=(200,))
    assert result["action"] == "drop"
    assert result["code"] == "VLAN_NATIVE_MISMATCH"


def test_mac_on_second_port_freezes_later_port_once():
    enforcer = _enforcer()
    _access(enforcer, 1)
    _access(enforcer, 2)
    enforcer.frame("aa", 100, 1, now=0.0)
    result = enforcer.frame("aa", 100, 2, now=0.5)
    assert result["action"] == "freeze"
    assert enforcer.is_frozen(2) is True
    assert enforcer.counters()["freeze_actions"] == 1


def test_frozen_port_rejects_new_addresses():
    enforcer = _enforcer()
    _access(enforcer, 1)
    _access(enforcer, 2)
    enforcer.frame("aa", 100, 1, now=0.0)
    enforcer.frame("aa", 100, 2, now=0.5)
    result = enforcer.frame("bb", 100, 2, now=0.6)
    assert result["action"] == "drop"
    assert result["code"] == "PORT_FROZEN"
    assert "bb" not in enforcer.sticky_table()


def test_max_address_count_allows_exact_limit():
    enforcer = _enforcer()
    _access(enforcer, 1, max_mac=2)
    assert enforcer.frame("aa", 100, 1)["action"] == "forward"
    assert enforcer.frame("bb", 100, 1)["action"] == "forward"
    result = enforcer.frame("cc", 100, 1)
    assert result["action"] == "drop"
    assert result["code"] == "PORT_SECURITY_VIOLATION"


def test_sticky_address_survives_port_down():
    enforcer = _enforcer()
    _access(enforcer, 1)
    enforcer.frame("aa", 100, 1)
    enforcer.port_down(1)
    assert enforcer.sticky_table()["aa"] == 1


def test_dtp_frame_dropped_and_port_not_trunked():
    enforcer = _enforcer()
    _access(enforcer, 1)
    result = enforcer.frame("aa", 100, 1, ethertype="dtp")
    assert result["action"] == "drop"
    assert result["code"] == "DTP_DENIED"


def test_priority_tagged_frame_is_treated_as_untagged():
    enforcer = _enforcer()
    _access(enforcer, 1, vlan=100)
    result = enforcer.frame("aa", 0, 1, tags=(0,))
    assert result["action"] == "forward"
    assert result["vlan"] == 100
    assert enforcer.sticky_table()["aa"] == 1


def test_dynamic_registration_frame_cannot_add_vlan():
    enforcer = _enforcer()
    _access(enforcer, 1, vlan=100)
    result = enforcer.frame("aa", 300, 1, ethertype="gvrp")
    assert result["action"] == "drop"
    assert result["code"] == "VLAN_DYNAMIC_DENIED"


def test_restart_restores_sticky_and_frozen_state(tmp_path):
    state = str(tmp_path / "vlan.json")
    enforcer = _enforcer(state)
    _access(enforcer, 1)
    _access(enforcer, 2)
    enforcer.frame("aa", 100, 1, now=0.0)
    enforcer.frame("aa", 100, 2, now=0.5)
    restarted = _enforcer(state)
    assert restarted.is_frozen(2) is True
    assert restarted.sticky_table()["aa"] == 1


def test_voice_vlan_is_not_open_to_data_pc():
    enforcer = _enforcer()
    _access(enforcer, 1, vlan=100, voice_vlan=None)
    result = enforcer.frame("aa", 200, 1, ethertype="voice")
    assert result["action"] == "drop"
    assert result["code"] == "VLAN_NOT_ALLOWED"


def test_untagged_normal_frame_is_forwarded():
    enforcer = _enforcer()
    _access(enforcer, 1, vlan=100)
    result = enforcer.frame("aa", 100, 1)
    assert result["action"] == "forward"
    assert result["code"] is None


def test_mac_move_between_ports_is_counted():
    enforcer = _enforcer()
    _access(enforcer, 1)
    _access(enforcer, 2)
    enforcer.frame("aa", 100, 1, now=0.0)
    enforcer.frame("aa", 100, 2, now=0.5)
    assert enforcer.counters()["mac_moves"] == 1


def test_release_unfreezes_port_and_allows_learning():
    enforcer = _enforcer()
    _access(enforcer, 1)
    _access(enforcer, 2)
    enforcer.frame("aa", 100, 1, now=0.0)
    enforcer.frame("aa", 100, 2, now=0.5)
    assert enforcer.is_frozen(2) is True
    enforcer.release(2)
    assert enforcer.is_frozen(2) is False
    assert enforcer.frame("cc", 100, 2, now=1.0)["action"] == "forward"


def test_second_address_move_inside_window_does_not_freeze_again():
    enforcer = _enforcer()
    _access(enforcer, 1)
    _access(enforcer, 2)
    _access(enforcer, 3)
    enforcer.frame("aa", 100, 1, now=0.0)
    enforcer.frame("aa", 100, 2, now=0.5)
    enforcer.frame("aa", 100, 3, now=0.8)
    assert enforcer.counters()["freeze_actions"] == 1
    assert enforcer.is_frozen(3) is False
