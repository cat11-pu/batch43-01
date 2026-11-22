import pytest

import vxlan_tunnel_registry


def _registry(**kwargs):
    registry = vxlan_tunnel_registry.VxlanTunnelRegistry(**kwargs)
    registry.register_vtep("vtep-a", "10.0.0.1")
    registry.register_vtep("vtep-b", "10.0.0.2")
    return registry


def test_vni_zero_is_rejected():
    registry = _registry()
    with pytest.raises(vxlan_tunnel_registry.VxlanError) as exc:
        registry.create_vni(0, "vtep-a")
    assert exc.value.code == "VXLAN_VNI_INVALID"


def test_vni_all_ones_is_rejected():
    registry = _registry()
    with pytest.raises(vxlan_tunnel_registry.VxlanError) as exc:
        registry.create_vni(0xFFFFFF, "vtep-a")
    assert exc.value.code == "VXLAN_VNI_INVALID"


def test_two_vteps_with_same_ip_conflict():
    registry = vxlan_tunnel_registry.VxlanTunnelRegistry()
    registry.register_vtep("vtep-a", "10.0.0.1")
    with pytest.raises(vxlan_tunnel_registry.VxlanError) as exc:
        registry.register_vtep("vtep-b", "10.0.0.1")
    assert exc.value.code == "VXLAN_VTEP_CONFLICT"


def test_vni_cannot_move_to_another_vtep():
    registry = _registry()
    registry.create_vni(100, "vtep-a")
    with pytest.raises(vxlan_tunnel_registry.VxlanError) as exc:
        registry.create_vni(100, "vtep-b")
    assert exc.value.code == "VXLAN_VNI_CONFLICT"


def test_mac_learning_is_isolated_per_vni():
    registry = _registry()
    registry.create_vni(100, "vtep-a")
    registry.create_vni(200, "vtep-a")
    registry.learn_mac(100, "aa:bb:cc:dd:ee:ff", "port-1")
    assert registry.lookup_mac(200, "aa:bb:cc:dd:ee:ff") is None


def test_same_mac_kept_separately_in_two_vnis():
    registry = _registry()
    registry.create_vni(100, "vtep-a")
    registry.create_vni(200, "vtep-a")
    registry.learn_mac(100, "aa:bb:cc:dd:ee:ff", "port-1")
    registry.learn_mac(200, "aa:bb:cc:dd:ee:ff", "port-9")
    assert registry.lookup_mac(100, "aa:bb:cc:dd:ee:ff") == "port-1"
    assert registry.lookup_mac(200, "aa:bb:cc:dd:ee:ff") == "port-9"


def test_relearning_mac_updates_single_entry():
    registry = _registry()
    registry.create_vni(100, "vtep-a")
    first = registry.learn_mac(100, "aa:bb:cc:dd:ee:ff", "port-1")
    second = registry.learn_mac(100, "aa:bb:cc:dd:ee:ff", "port-2")
    assert first["entries"] == 1
    assert second["entries"] == 1
    assert registry.lookup_mac(100, "aa:bb:cc:dd:ee:ff") == "port-2"


def test_delete_vni_removes_multicast_membership():
    registry = _registry()
    registry.create_vni(100, "vtep-a", group="239.1.1.1")
    result = registry.delete_vni(100)
    assert result["multicast_memberships_removed"] == 1
    assert 100 not in registry.multicast_groups()


def test_delete_vni_clears_arp_suppression():
    registry = _registry()
    registry.create_vni(100, "vtep-a")
    registry.cache_arp(100, "10.10.0.5", "aa:bb:cc:dd:ee:ff")
    registry.delete_vni(100)
    assert registry.lookup_arp(100, "10.10.0.5") is None


def test_encapsulation_adds_fifty_byte_overhead():
    registry = _registry()
    result = registry.encapsulate(1400)
    assert result["overhead"] == 50
    assert result["outer_size"] == 1450
    assert result["inner_size"] == 1400


def test_source_port_hash_is_stable_and_spread():
    registry = _registry()
    first = registry.source_port_hash("flow-a")
    assert first == registry.source_port_hash("flow-a")
    assert 49152 <= first <= 65535
    assert 49152 <= registry.source_port_hash("flow-b") <= 65535
    assert registry.source_port_hash("flow-a") != registry.source_port_hash("flow-b")


def test_vtep_flap_is_rate_limited():
    registry = _registry(flap_limit=3, flap_window=60)
    registry.update_remote("vtep-b", "10.0.0.3", now=0)
    registry.update_remote("vtep-b", "10.0.0.4", now=10)
    registry.update_remote("vtep-b", "10.0.0.5", now=20)
    with pytest.raises(vxlan_tunnel_registry.VxlanError) as exc:
        registry.update_remote("vtep-b", "10.0.0.6", now=30)
    assert exc.value.code == "VXLAN_VTEP_FLAP"


def test_reserved_bits_in_header_are_rejected():
    registry = _registry()
    with pytest.raises(vxlan_tunnel_registry.VxlanError) as exc:
        registry.validate_encap({"vni": 100, "reserved_bits": 3})
    assert exc.value.code == "VXLAN_RESERVED_BITS"


def test_restart_restores_forwarding_before_flooding():
    registry = _registry()
    registry.create_vni(100, "vtep-a", group="239.1.1.1")
    registry.learn_mac(100, "aa:bb:cc:dd:ee:ff", "port-1")
    registry.update_remote("vtep-b", "10.0.0.7", now=0)
    state = registry.dump_state()
    restarted = vxlan_tunnel_registry.VxlanTunnelRegistry()
    restarted.load_state(state)
    assert restarted.is_ready() is True
    assert restarted.flooding_required() is False
    assert restarted.lookup_mac(100, "aa:bb:cc:dd:ee:ff") == "port-1"


def test_delete_vni_is_idempotent_without_half_entries():
    registry = _registry()
    registry.create_vni(100, "vtep-a", group="239.1.1.1")
    registry.learn_mac(100, "aa:bb:cc:dd:ee:ff", "port-1")
    first = registry.delete_vni(100)
    second = registry.delete_vni(100)
    assert first["deleted"] is True
    assert second["deleted"] is False
    assert registry.lookup_mac(100, "aa:bb:cc:dd:ee:ff") is None
    assert registry.multicast_groups() == {}
