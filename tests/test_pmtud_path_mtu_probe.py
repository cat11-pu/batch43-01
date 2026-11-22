import pmtud_path_mtu_probe


FLOW = ("10.0.0.1", "10.9.0.1", 1234, 443, "tcp")


def _probe():
    return pmtud_path_mtu_probe.PmtudPathMtuProbe(default_mtu=1500, cache_ttl=60.0)


def test_icmp_with_short_quote_is_unverified():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, now=0.0)
    result = probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, quoted_bytes=4, now=1.0)
    assert result["accepted"] is False
    assert result["code"] == "PMTU_ICMP_UNVERIFIED"
    assert probe.mtu_for("10.9.0.1", now=1.0) == 1500


def test_icmp_without_recent_matching_flow_is_unverified():
    probe = _probe()
    result = probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, now=5.0)
    assert result["accepted"] is False
    assert result["code"] == "PMTU_ICMP_UNVERIFIED"


def test_verified_icmp_updates_path_cache():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, now=0.0)
    result = probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, quoted_bytes=8, now=1.0)
    assert result["accepted"] is True
    assert result["mtu"] == 1300
    assert probe.mtu_for("10.9.0.1", now=1.0) == 1300


def test_ipv6_packet_too_big_never_goes_below_1280():
    probe = _probe()
    flow6 = ("fd00::1", "fd00::2", 1234, 443, "tcp")
    probe.send(flow6, "fd00::2", 1400, now=0.0)
    result = probe.on_icmp(
        flow6, "fd00::2", reported_mtu=1000, quoted_bytes=8, icmp_type=2, icmp_code=0, now=1.0
    )
    assert result["accepted"] is True
    assert result["mtu"] == 1280
    assert probe.mtu_for("fd00::2", now=1.0) == 1280


def test_route_change_invalidates_cached_value():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, now=0.0)
    probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, now=1.0)
    assert probe.mtu_for("10.9.0.1", now=1.0) == 1300
    probe.route_change("10.9.0.1")
    assert probe.mtu_for("10.9.0.1", now=1.0) == 1500


def test_restored_expired_low_value_is_not_reused():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, now=0.0)
    probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, now=1.0)
    state = probe.snapshot()
    restarted = _probe()
    restarted.restore(state, now=100.0)
    assert restarted.mtu_for("10.9.0.1", now=100.0) == 1500


def test_tunnel_encapsulation_caches_are_separate():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, tunnel="vxlan-100", now=0.0)
    probe.on_icmp(FLOW, "10.9.0.1", tunnel="vxlan-100", reported_mtu=1300, now=1.0)
    assert probe.mtu_for("10.9.0.1", tunnel="vxlan-100", now=1.0) == 1300
    assert probe.mtu_for("10.9.0.1", tunnel="vxlan-200", now=1.0) == 1500


def test_df_packet_over_limit_reports_fragmentation_needed():
    probe = _probe()
    result = probe.send(FLOW, "10.9.0.1", 1600, df=True, now=0.0)
    assert result["action"] == "fragmentation_needed"
    assert result["code"] == "PMTU_FRAG_NEEDED"
    assert result["icmp_sent"] is True


def test_non_df_packet_over_limit_may_be_fragmented():
    probe = _probe()
    result = probe.send(FLOW, "10.9.0.1", 1600, df=False, now=0.0)
    assert result["action"] == "send"
    assert result["fragmented"] is True


def test_mss_clamp_applies_to_both_directions():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, now=0.0)
    probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, now=1.0)
    assert probe.clamp_mss("10.9.0.1", 1460, direction="out", now=1.0) == 1260
    assert probe.clamp_mss("10.9.0.1", 1460, direction="in", now=1.0) == 1260


def test_cache_entry_expires_after_ttl():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, now=0.0)
    probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, now=1.0)
    assert probe.mtu_for("10.9.0.1", now=100.0) == 1500


def test_wrong_icmpv6_type_is_ignored():
    probe = _probe()
    flow6 = ("fd00::1", "fd00::2", 1234, 443, "tcp")
    probe.send(flow6, "fd00::2", 1400, now=0.0)
    result = probe.on_icmp(
        flow6, "fd00::2", reported_mtu=1000, quoted_bytes=8, icmp_type=1, icmp_code=0, now=1.0
    )
    assert result["accepted"] is False
    assert result["code"] == "PMTU_ICMP_IGNORED"
    assert probe.mtu_for("fd00::2", now=1.0) == 1500


def test_application_timeout_does_not_lower_path_mtu():
    probe = _probe()
    probe.report_application_timeout("10.9.0.1")
    assert probe.mtu_for("10.9.0.1", now=0.0) == 1500


def test_destination_cache_entries_are_independent():
    probe = _probe()
    probe.send(FLOW, "10.9.0.1", 1400, now=0.0)
    probe.on_icmp(FLOW, "10.9.0.1", reported_mtu=1300, now=1.0)
    assert probe.mtu_for("10.9.0.1", now=1.0) == 1300
    assert probe.mtu_for("10.9.0.2", now=1.0) == 1500


def test_packet_equal_to_path_mtu_is_sent():
    probe = _probe()
    result = probe.send(FLOW, "10.9.0.1", 1500, df=True, now=0.0)
    assert result["action"] == "send"
    assert result["code"] is None
