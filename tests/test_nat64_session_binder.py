import pytest

import nat64_session_binder


def _binder(**kwargs):
    return nat64_session_binder.Nat64SessionBinder(**kwargs)


def test_same_five_tuple_reuses_one_mapping():
    binder = _binder()
    first = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 53,
                                  "udp", now=0)
    second = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 53,
                                   "udp", now=1)
    assert second["mapping_id"] == first["mapping_id"]
    assert second["public_port"] == first["public_port"]


def test_different_source_ports_get_different_mappings():
    binder = _binder()
    first = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 443,
                                  "tcp", now=0)
    second = binder.create_session("192.168.1.10", 5001, "203.0.113.8", 443,
                                   "tcp", now=0)
    assert first["mapping_id"] != second["mapping_id"]
    assert first["public_port"] != second["public_port"]


def test_464xlat_preserves_port_parity():
    binder = _binder()
    even = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 80,
                                 "tcp", xlat464=True)
    odd = binder.create_session("192.168.1.10", 5001, "203.0.113.8", 80,
                                "tcp", xlat464=True)
    assert even["public_port"] % 2 == 0
    assert odd["public_port"] % 2 == 1


def test_464xlat_rejects_wrong_requested_parity():
    binder = _binder()
    with pytest.raises(nat64_session_binder.Nat64Error) as exc:
        binder.create_session("192.168.1.10", 5000, "203.0.113.8", 80, "tcp",
                              xlat464=True, requested_port=5001)
    assert exc.value.code == "NAT64_PORT_PARITY"


def test_internal_destination_is_hairpinned():
    binder = _binder()
    result = binder.create_session("192.168.1.10", 5000, "192.168.1.20", 8080,
                                   "tcp")
    assert result["hairpin"] is True
    assert result["translated"] is True
    assert isinstance(result["inbound_port"], int)


def test_fragments_are_reassembled_before_translation():
    binder = _binder()
    mapping = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 53,
                                    "udp")
    first = binder.translate_fragment(mapping["mapping_id"], 0, True, b"aaaa")
    assert first["buffered"] is True
    done = binder.translate_fragment(mapping["mapping_id"], 8, False, b"bb")
    assert done["translated"] is True
    assert done["offset"] == 8
    assert done["total_size"] == 6


def test_icmp_error_reverse_lookup_finds_session():
    binder = _binder()
    mapping = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 53,
                                    "udp")
    result = binder.handle_icmp_error("192.168.1.10", 5000, "203.0.113.8", 53,
                                      "udp")
    assert result["mapping_id"] == mapping["mapping_id"]
    assert result["delivered"] is True


def test_icmp_error_without_session_is_dropped():
    binder = _binder()
    with pytest.raises(nat64_session_binder.Nat64Error) as exc:
        binder.handle_icmp_error("192.168.1.99", 4040, "203.0.113.8", 53, "udp")
    assert exc.value.code == "NAT64_NO_SESSION"


def test_tcp_reset_removes_mapping():
    binder = _binder()
    binder.create_session("192.168.1.10", 5000, "203.0.113.8", 443, "tcp")
    result = binder.handle_tcp_reset("192.168.1.10", 5000, "203.0.113.8", 443)
    assert result["removed"] is True
    assert binder.lookup_session("192.168.1.10", 5000, "203.0.113.8", 443,
                                 "tcp") is None


def test_restart_restores_mapping_and_ttl():
    binder = _binder()
    mapping = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 443,
                                    "tcp", now=100)
    state = binder.dump_state()
    restarted = _binder()
    restarted.load_state(state)
    assert restarted.lookup_session("192.168.1.10", 5000, "203.0.113.8", 443,
                                    "tcp") is not None
    assert restarted.remaining_ttl(mapping["mapping_id"], now=101) > 0


def test_dns64_address_is_scoped():
    binder = _binder()
    address = binder.synthesize_dns64("203.0.113.5", scope="tenant-a")
    assert "64:ff9b" in address
    assert binder.dns64_usable(address, "tenant-a") is True
    assert binder.dns64_usable(address, "tenant-b") is False


def test_protocol_specific_idle_timeouts():
    binder = _binder()
    assert binder.should_expire("udp", 300) is True
    assert binder.should_expire("udp", 299) is False
    assert binder.should_expire("tcp", 300) is False


def test_public_ports_are_never_shared():
    binder = _binder(port_min=2048, port_max=2050)
    first = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 80,
                                  "tcp")
    second = binder.create_session("192.168.1.11", 5000, "203.0.113.8", 80,
                                   "tcp")
    assert first["public_port"] != second["public_port"]
    assert 2048 <= first["public_port"] <= 2050
    assert 2048 <= second["public_port"] <= 2050


def test_expire_removes_udp_but_keeps_tcp():
    binder = _binder()
    udp = binder.create_session("192.168.1.10", 5000, "203.0.113.8", 53,
                                "udp", now=0)
    binder.create_session("192.168.1.10", 5001, "203.0.113.8", 443, "tcp",
                          now=0)
    result = binder.expire(now=301)
    assert result["expired"] == 1
    assert binder.lookup_session("192.168.1.10", 5000, "203.0.113.8", 53,
                                 "udp") is None
    assert binder.lookup_session("192.168.1.10", 5001, "203.0.113.8", 443,
                                 "tcp") is not None


def test_unknown_flow_lookup_returns_none():
    binder = _binder()
    assert binder.lookup_session("192.168.1.99", 4040, "203.0.113.8", 53,
                                 "udp") is None
