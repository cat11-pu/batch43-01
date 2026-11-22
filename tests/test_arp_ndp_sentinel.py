import arp_ndp_sentinel


def _sentinel(state_path=None):
    return arp_ndp_sentinel.ArpNdpSentinel(state_path=state_path)


def test_gratuitous_arp_cannot_override_static_binding():
    sentinel = _sentinel()
    sentinel.add_static(10, "10.0.0.1", "aa:aa:aa:aa:aa:aa", port=1)
    result = sentinel.observe_arp(10, "10.0.0.1", "bb:bb:bb:bb:bb:bb", 2, gratuitous=True)
    assert result["action"] == "blocked"
    assert result["code"] == "ARP_BINDING_LOCKED"
    assert sentinel.table(10)["10.0.0.1"] == "aa:aa:aa:aa:aa:aa"


def test_binding_locked_counter_increments_per_attempt():
    sentinel = _sentinel()
    sentinel.add_static(10, "10.0.0.1", "aa:aa:aa:aa:aa:aa", port=1)
    sentinel.observe_arp(10, "10.0.0.1", "bb:bb:bb:bb:bb:bb", 2, gratuitous=True)
    sentinel.observe_arp(10, "10.0.0.1", "cc:cc:cc:cc:cc:cc", 3, gratuitous=True)
    assert sentinel.counters()["arp_binding_locked"] == 2


def test_ndp_override_flag_cannot_replace_static_entry():
    sentinel = _sentinel()
    sentinel.add_static(10, "fd00::1", "aa:aa:aa:aa:aa:aa", port=1)
    result = sentinel.observe_ndp(10, "fd00::1", "dd:dd:dd:dd:dd:dd", 2, override_flag=True)
    assert result["action"] == "blocked"
    assert result["code"] == "ARP_BINDING_LOCKED"
    assert sentinel.table(10)["fd00::1"] == "aa:aa:aa:aa:aa:aa"


def test_two_macs_for_same_address_isolate_later_port_once():
    sentinel = _sentinel()
    sentinel.observe_arp(10, "10.0.0.20", "aa:aa:aa:aa:aa:aa", 1, now=0.0)
    result = sentinel.observe_arp(10, "10.0.0.20", "bb:bb:bb:bb:bb:bb", 2, now=0.5)
    assert result["action"] == "isolate"
    assert sentinel.is_blocked(2) is True
    assert sentinel.counters()["isolation_actions"] == 1


def test_third_conflict_inside_window_does_not_isolate_again():
    sentinel = _sentinel()
    sentinel.observe_arp(10, "10.0.0.21", "aa:aa:aa:aa:aa:aa", 1, now=0.0)
    sentinel.observe_arp(10, "10.0.0.21", "bb:bb:bb:bb:bb:bb", 2, now=0.5)
    sentinel.observe_arp(10, "10.0.0.21", "cc:cc:cc:cc:cc:cc", 3, now=0.7)
    assert sentinel.counters()["isolation_actions"] == 1
    assert sentinel.is_blocked(3) is False


def test_restart_restores_bindings_and_isolation(tmp_path):
    state = str(tmp_path / "sentinel.json")
    sentinel = _sentinel(state)
    sentinel.add_static(10, "10.0.0.1", "aa:aa:aa:aa:aa:aa", port=1)
    sentinel.observe_arp(10, "10.0.0.30", "bb:bb:bb:bb:bb:bb", 1, now=0.0)
    sentinel.observe_arp(10, "10.0.0.30", "cc:cc:cc:cc:cc:cc", 2, now=0.5)
    restarted = _sentinel(state)
    assert restarted.is_blocked(2) is True
    assert restarted.table(10)["10.0.0.1"] == "aa:aa:aa:aa:aa:aa"


def test_release_port_reenables_it():
    sentinel = _sentinel()
    sentinel.observe_arp(10, "10.0.0.31", "aa:aa:aa:aa:aa:aa", 1, now=0.0)
    sentinel.observe_arp(10, "10.0.0.31", "bb:bb:bb:bb:bb:bb", 2, now=0.5)
    assert sentinel.is_blocked(2) is True
    sentinel.release_port(2)
    assert sentinel.is_blocked(2) is False


def test_same_address_in_different_vlans_is_independent():
    sentinel = _sentinel()
    sentinel.add_static(10, "10.0.0.1", "aa:aa:aa:aa:aa:aa", port=1)
    result = sentinel.observe_arp(20, "10.0.0.1", "bb:bb:bb:bb:bb:bb", 2)
    assert result["action"] == "accept"
    assert sentinel.table(10)["10.0.0.1"] == "aa:aa:aa:aa:aa:aa"


def test_unspecified_source_does_not_enter_neighbor_state():
    sentinel = _sentinel()
    result = sentinel.observe_ndp(10, "fd00::9", "aa:aa:aa:aa:aa:aa", 1, src_unspecified=True)
    assert result["action"] == "ignore"
    assert "fd00::9" not in sentinel.table(10)


def test_binding_flood_does_not_evict_static_entry():
    sentinel = _sentinel()
    sentinel.add_static(10, "10.0.0.1", "aa:aa:aa:aa:aa:aa", port=1)
    for index in range(200):
        sentinel.observe_arp(10, "10.0.1.%d" % index, "ee:ee:ee:ee:ee:%02x" % index, 7, now=float(index))
    assert sentinel.table(10)["10.0.0.1"] == "aa:aa:aa:aa:aa:aa"


def test_snooped_binding_participates_in_validation():
    sentinel = _sentinel()
    sentinel.add_snooping(10, "10.0.0.50", "cc:cc:cc:cc:cc:cc", 5)
    result = sentinel.observe_arp(10, "10.0.0.50", "dd:dd:dd:dd:dd:dd", 6)
    assert result["action"] == "blocked"
    assert result["code"] == "ARP_BINDING_LOCKED"


def test_mac_moving_between_ports_is_counted():
    sentinel = _sentinel()
    sentinel.observe_arp(10, "10.0.0.60", "aa:aa:aa:aa:aa:aa", 1, now=0.0)
    result = sentinel.observe_arp(10, "10.0.0.61", "aa:aa:aa:aa:aa:aa", 2, now=0.5)
    assert result["action"] == "move"
    assert sentinel.counters()["mac_moves"] == 1


def test_allowlisted_non_router_host_router_flag_denied():
    sentinel = _sentinel()
    sentinel.add_allowlist("aa:aa:aa:aa:aa:aa")
    result = sentinel.observe_ndp(10, "fd00::2", "aa:aa:aa:aa:aa:aa", 1, router_flag=True)
    assert result["action"] == "drop"
    assert result["code"] == "NDP_ROUTER_FLAG_DENIED"


def test_ordinary_host_ndp_is_accepted_and_learned():
    sentinel = _sentinel()
    result = sentinel.observe_ndp(10, "fd00::3", "bb:bb:bb:bb:bb:bb", 1, router_flag=False)
    assert result["action"] == "accept"
    assert sentinel.table(10)["fd00::3"] == "bb:bb:bb:bb:bb:bb"


def test_isolated_port_frames_are_blocked():
    sentinel = _sentinel()
    sentinel.observe_arp(10, "10.0.0.70", "aa:aa:aa:aa:aa:aa", 1, now=0.0)
    sentinel.observe_arp(10, "10.0.0.70", "bb:bb:bb:bb:bb:bb", 2, now=0.5)
    blocked = sentinel.observe_arp(10, "10.0.0.71", "cc:cc:cc:cc:cc:cc", 2, now=0.6)
    assert blocked["action"] == "blocked"
    assert blocked["code"] == "PORT_ISOLATED"
