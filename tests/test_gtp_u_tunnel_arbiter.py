import pytest

import gtp_u_tunnel_arbiter


def _session(gw, teid=100, ue="10.0.0.5", src="10.1.1.1", charging="c-1"):
    gw.add_session(teid, ue, src, charging_id=charging)


def test_unknown_teid_dropped_counted_and_no_icmp():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    result = gw.forward_uplink(999, 1, src_ip="10.1.1.1")
    assert result["accepted"] is False
    assert result["code"] == "GTP_UNKNOWN_TEID"
    assert result["icmp"] is False
    assert gw.stats()["unknown_teid"] == 1


def test_duplicate_uplink_sequence_is_silently_dropped():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter(replay_window=4)
    _session(gw)
    assert gw.forward_uplink(100, 10, src_ip="10.1.1.1")["accepted"] is True
    duplicate = gw.forward_uplink(100, 10, src_ip="10.1.1.1")
    assert duplicate["accepted"] is False
    assert duplicate["code"] == "GTP_DUPLICATE"
    assert gw.stats()["duplicate"] == 1


def test_duplicate_does_not_advance_replay_window():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter(replay_window=8)
    _session(gw)
    gw.forward_uplink(100, 10, src_ip="10.1.1.1")
    gw.forward_uplink(100, 10, src_ip="10.1.1.1")
    assert gw.forward_uplink(100, 9, src_ip="10.1.1.1")["accepted"] is True


def test_stale_sequence_outside_window_rejected():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter(replay_window=4)
    _session(gw)
    gw.forward_uplink(100, 20, src_ip="10.1.1.1")
    gw.forward_uplink(100, 21, src_ip="10.1.1.1")
    stale = gw.forward_uplink(100, 20, src_ip="10.1.1.1")
    assert stale["accepted"] is False
    assert stale["code"] == "GTP_DUPLICATE"


def test_restored_checkpoint_still_rejects_old_sequence():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw)
    gw.forward_uplink(100, 5, src_ip="10.1.1.1")
    state = gw.checkpoint()
    restarted = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    restarted.restore(state)
    replay = restarted.forward_uplink(100, 5, src_ip="10.1.1.1")
    assert replay["accepted"] is False
    assert replay["code"] == "GTP_DUPLICATE"
    assert restarted.forward_uplink(100, 6, src_ip="10.1.1.1")["accepted"] is True


def test_restored_checkpoint_restores_sessions():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=200, ue="10.0.0.9", src="10.2.2.2", charging="c-9")
    state = gw.checkpoint()
    restarted = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    restarted.restore(state)
    assert restarted.forward_downlink(200, "10.0.0.9", b"data")["accepted"] is True


def test_downlink_ue_ip_mismatch_denied():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=300, ue="10.0.0.5", src="10.1.1.1", charging="c-3")
    result = gw.forward_downlink(300, "10.0.0.99", b"data")
    assert result["accepted"] is False
    assert result["code"] == "GTP_UE_IP_MISMATCH"
    assert gw.stats()["ue_ip_mismatch"] == 1


def test_downlink_matching_ue_ip_is_forwarded():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=301, ue="10.0.0.6", src="10.1.1.1", charging="c-4")
    result = gw.forward_downlink(301, "10.0.0.6", b"payload")
    assert result["accepted"] is True
    assert result["code"] is None
    assert gw.stats()["delivered"] == 1


def test_extension_headers_parsed_by_own_length():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    headers = [
        {"type": 0x85, "length": 4, "value": b"abcd"},
        {"type": 0x01, "length": 8, "value": b"12345678"},
    ]
    parsed = gw.walk_extensions(headers)
    assert [item["value"] for item in parsed] == [b"abcd", b"12345678"]
    assert [item["type"] for item in parsed] == [0x85, 0x01]


def test_unsolicited_source_address_rejected():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=400, ue="10.0.0.7", src="10.3.3.3", charging="c-5")
    result = gw.forward_uplink(400, 1, src_ip="203.0.113.9")
    assert result["accepted"] is False
    assert result["code"] == "GTP_UNSOLICITED_SOURCE"


def test_charging_identifier_reuse_rejected():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=500, ue="10.0.0.10", src="10.4.4.4", charging="charge-1")
    with pytest.raises(gtp_u_tunnel_arbiter.GtpError) as exc:
        gw.add_session(501, "10.0.0.11", "10.4.4.5", charging_id="charge-1")
    assert exc.value.code == "GTP_CHARGING_ID_REUSED"


def test_non_first_fragment_still_validates_source():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=600, ue="10.0.0.12", src="10.5.5.5", charging="c-6")
    result = gw.forward_uplink(600, 1, src_ip="198.51.100.7", fragment_index=1)
    assert result["accepted"] is False
    assert result["code"] == "GTP_UNSOLICITED_SOURCE"


def test_version_field_cannot_be_treated_as_extension_flag():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=700, ue="10.0.0.13", src="10.6.6.6", charging="c-7")
    result = gw.forward_uplink(700, 1, src_ip="10.6.6.6", version=2)
    assert result["accepted"] is False
    assert result["code"] == "GTP_BAD_VERSION"


def test_echo_reply_only_for_known_peer():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    _session(gw, teid=800, ue="10.0.0.14", src="10.7.7.7", charging="c-8")
    assert gw.handle_echo("203.0.113.200", is_request=True) is False
    assert gw.handle_echo("10.7.7.7", is_request=True) is True


def test_repeated_unknown_teid_packets_all_counted():
    gw = gtp_u_tunnel_arbiter.GtpUTunnelArbiter()
    for seq in range(1, 6):
        assert gw.forward_uplink(999, seq)["code"] == "GTP_UNKNOWN_TEID"
    assert gw.stats()["unknown_teid"] == 5
