import ipv6_ext_filter


def _filter():
    return ipv6_ext_filter.Ipv6ExtFilter()


def test_hop_by_hop_after_routing_header_rejected():
    result = _filter().filter_packet(headers=[{"type": 43}, {"type": 0}])
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_EXT_ORDER"


def test_routing_header_type_zero_denied_with_icmp_parameter_problem():
    result = _filter().filter_packet(headers=[{"type": 43, "routing_type": 0}])
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_RH0_DENIED"
    assert result["icmp"] == {"type": 4, "code": 0}


def test_extension_chain_longer_than_limit_rejected():
    headers = [{"type": 60} for _ in range(9)]
    result = _filter().filter_packet(headers=headers)
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_EXT_CHAIN_TOO_LONG"


def test_unknown_destination_option_with_action_bits_denied():
    headers = [{"type": 60, "options": [{"type": 0x3E, "action": 3}]}]
    result = _filter().filter_packet(headers=headers)
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_UNKNOWN_OPTION"


def test_two_fragment_headers_in_one_packet_rejected():
    result = _filter().filter_packet(headers=[{"type": 44}, {"type": 44}])
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_DOUBLE_FRAG"


def test_flow_label_out_of_range_rejected():
    result = _filter().filter_packet(headers=[{"type": 60}], flow_label=0x100000)
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_FLOW_LABEL_INVALID"


def test_jumbo_payload_option_larger_than_packet_rejected():
    headers = [{"type": 0, "options": [{"type": 0xC2, "length": 4, "value": 70000}]}]
    result = _filter().filter_packet(headers=headers, payload_len=100)
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_JUMBO_INVALID"


def test_overlapping_fragments_dropped_and_not_queued():
    ext = _filter()
    ext.reassemble("2001:db8::1", 7, 0, b"01234567", more=True)
    overlap = ext.reassemble("2001:db8::1", 7, 4, b"XXXX", more=True)
    assert overlap["code"] == "IPV6_FRAG_OVERLAP"
    assert overlap["complete"] is False
    fresh = ext.reassemble("2001:db8::1", 8, 0, b"abcd", more=False)
    assert fresh["complete"] is True


def test_non_overlapping_fragments_are_reassembled():
    ext = _filter()
    assert ext.reassemble("2001:db8::1", 1, 0, b"AAAA", more=True)["complete"] is False
    result = ext.reassemble("2001:db8::1", 1, 4, b"BBBB", more=False)
    assert result["complete"] is True
    assert result["data"] == b"AAAABBBB"


def test_reassembly_queues_are_isolated_by_source():
    ext = _filter()
    ext.reassemble("2001:db8::1", 9, 0, b"1111", more=True)
    other = ext.reassemble("2001:db8::2", 9, 0, b"2222", more=False)
    assert other["complete"] is True
    assert other["data"] == b"2222"


def test_orphan_icmpv6_error_dropped():
    ext = _filter()
    ext.register_flow("fd00::1", "fd00::2", 1234, 53, proto="udp")
    orphan = {
        "type": 137,
        "code": 0,
        "src": "fd00::9",
        "dst": "fd00::2",
        "sport": 4444,
        "dport": 53,
        "proto": "udp",
    }
    result = ext.filter_packet(headers=[], icmp_error=orphan)
    assert result["action"] == "drop"
    assert result["code"] == "ICMPV6_ORPHAN"


def test_icmpv6_error_matching_active_flow_accepted():
    ext = _filter()
    ext.register_flow("fd00::1", "fd00::2", 1234, 53, proto="udp")
    matching = {
        "type": 137,
        "code": 0,
        "src": "fd00::1",
        "dst": "fd00::2",
        "sport": 1234,
        "dport": 53,
        "proto": "udp",
    }
    result = ext.filter_packet(headers=[], icmp_error=matching)
    assert result["action"] == "forward"
    assert result["code"] is None


def test_atomic_fragment_does_not_bypass_routing_header_check():
    fragment = {"offset": 0, "more": False, "ident": 1, "atomic": True}
    result = _filter().filter_packet(headers=[{"type": 43, "routing_type": 0}], fragment=fragment)
    assert result["action"] == "drop"
    assert result["code"] == "IPV6_RH0_DENIED"


def test_routing_header_type_one_is_allowed():
    result = _filter().filter_packet(headers=[{"type": 43, "routing_type": 1}])
    assert result["action"] == "forward"
    assert result["code"] is None


def test_exactly_eight_extension_headers_accepted():
    headers = [{"type": 60} for _ in range(8)]
    result = _filter().filter_packet(headers=headers)
    assert result["action"] == "forward"
