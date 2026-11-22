import coap_oscore_gateway


def _request(gw, sender="meter-1", iv=1, token=b"tok", **kwargs):
    return gw.handle_request("ep-1", sender, iv, token=token, **kwargs)


def test_same_sender_and_partial_iv_is_replay():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    first = _request(gw, iv=10)
    second = _request(gw, iv=10)
    assert first["code"] == "2.05"
    assert first["executed"] is True
    assert second["code"] == "4.01"
    assert second["executed"] is False
    assert gw.counters()["replay"] == 1


def test_different_senders_may_use_same_partial_iv():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    assert _request(gw, sender="meter-1", iv=10)["code"] == "2.05"
    assert _request(gw, sender="meter-2", iv=10)["code"] == "2.05"


def test_restart_rejects_sequence_seen_before(tmp_path):
    state = str(tmp_path / "oscore-state.json")
    first = coap_oscore_gateway.CoapOscoreGateway(state_path=state)
    assert _request(first, iv=5)["code"] == "2.05"
    restarted = coap_oscore_gateway.CoapOscoreGateway(state_path=state)
    replay = _request(restarted, iv=5)
    assert replay["code"] == "4.01"
    assert replay["executed"] is False


def test_restart_accepts_next_sequence_after_saved_value(tmp_path):
    state = str(tmp_path / "oscore-state.json")
    first = coap_oscore_gateway.CoapOscoreGateway(state_path=state)
    _request(first, iv=5)
    restarted = coap_oscore_gateway.CoapOscoreGateway(state_path=state)
    assert _request(restarted, iv=6)["code"] == "2.05"


def test_restart_rejects_sequence_below_saved_value(tmp_path):
    state = str(tmp_path / "oscore-state.json")
    first = coap_oscore_gateway.CoapOscoreGateway(state_path=state)
    _request(first, iv=8)
    restarted = coap_oscore_gateway.CoapOscoreGateway(state_path=state)
    assert _request(restarted, iv=4)["code"] == "4.01"


def test_zero_length_token_rejected():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    result = _request(gw, token=b"")
    assert result["code"] == "4.00"
    assert result["executed"] is False


def test_nine_byte_token_rejected():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    result = _request(gw, token=b"123456789")
    assert result["code"] == "4.00"
    assert result["executed"] is False


def test_multicast_without_group_key_rejected():
    gw = coap_oscore_gateway.CoapOscoreGateway(group_key_id="group-7")
    result = _request(gw, sender="meter-1", multicast=True, group_key_id=None)
    assert result["code"].startswith("4.")
    assert result["executed"] is False


def test_multicast_with_matching_group_key_accepted():
    gw = coap_oscore_gateway.CoapOscoreGateway(group_key_id="group-7")
    result = _request(gw, sender="meter-1", multicast=True, group_key_id="group-7")
    assert result["code"] == "2.05"
    assert result["executed"] is True


def test_block_key_mismatch_returns_4_08():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    buffer_id = gw.begin_block("ep-1", b"tok", content_format=0)
    result = gw.add_block(buffer_id, b"chunk", content_format=1)
    assert result["code"] == "4.08"


def test_concurrent_block_buffers_do_not_share_data():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    first = gw.begin_block("ep-1", b"token-a", content_format=0)
    second = gw.begin_block("ep-1", b"token-b", content_format=0)
    gw.add_block(first, b"AAAA")
    gw.add_block(second, b"BBBB")
    assert gw.add_block(first, b"1111")["data"] == b"AAAA1111"
    assert gw.add_block(second, b"2222")["data"] == b"BBBB2222"


def test_cancel_observe_stops_notifications():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    token = gw.observe("ep-1", b"obs", "/power")
    assert gw.notify(token, b"reading") is True
    gw.cancel_observe(token)
    assert gw.notify(token, b"reading") is False
    assert gw.notifications(token) == [b"reading"]


def test_reset_releases_observation_registration():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    token = gw.observe("ep-1", b"obs-reset", "/power")
    gw.reset("ep-1", b"obs-reset")
    assert gw.notify(token, b"reading") is False
    assert gw.notifications(token) == []


def test_observation_notification_is_bound_to_token():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    first = gw.observe("ep-1", b"token-1", "/meter")
    second = gw.observe("ep-1", b"token-2", "/meter")
    assert gw.notify(first, b"for-first") is True
    assert gw.notifications(first) == [b"for-first"]
    assert gw.notifications(second) == []


def test_replay_counter_accumulates():
    gw = coap_oscore_gateway.CoapOscoreGateway()
    _request(gw, sender="meter-1", iv=1)
    _request(gw, sender="meter-1", iv=1)
    _request(gw, sender="meter-1", iv=2)
    _request(gw, sender="meter-1", iv=2)
    assert gw.counters()["replay"] == 2
