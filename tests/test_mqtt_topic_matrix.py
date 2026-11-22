import pytest

import mqtt_topic_matrix


def _code(exc):
    return getattr(exc.value, "code", str(exc.value))


def _connected(gw, client_id="c1", username="dev", tls_cn=None):
    return gw.connect(client_id, username, tls_cn=tls_cn)


def test_root_wildcard_cannot_escape_tenant_prefix():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw)
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.subscribe("c1", "#")
    assert _code(exc) == "SUB_DENIED"


def test_other_tenant_wildcard_denied():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw)
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.subscribe("c1", "tenant-b/#")
    assert _code(exc) == "SUB_DENIED"


def test_system_topic_blocked_for_normal_device():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw)
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.subscribe("c1", "$SYS/#")
    assert _code(exc) == "SYS_TOPIC_DENIED"
    assert any(event.get("code") == "SYS_TOPIC_DENIED" for event in gw.audit_events())


def test_acl_change_is_effective_without_restart():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw)
    assert gw.subscribe("c1", "tenant-a/secret/#") is True
    gw.add_acl("dev", "tenant-a/secret/#", allow=False)
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.subscribe("c1", "tenant-a/secret/#")
    assert _code(exc) == "SUB_DENIED"


def test_qos2_duplicate_packet_id_is_delivered_once():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw, "sub")
    gw.subscribe("sub", "tenant-a/#")
    _connected(gw, "pub")
    first = gw.publish("pub", "tenant-a/meter/1", b"reading", qos=2, packet_id=7)
    second = gw.publish("pub", "tenant-a/meter/1", b"reading", qos=2, packet_id=7)
    assert first == 1
    assert second == 0
    messages = gw.take_messages("sub")
    assert len(messages) == 1
    assert messages[0][0] == "tenant-a/meter/1"


def test_second_connection_for_client_id_disconnects_old_session():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    old_token = _connected(gw, "dup")
    assert gw.is_active("dup", old_token) is True
    new_token = _connected(gw, "dup")
    assert gw.is_active("dup", old_token) is False
    assert gw.is_active("dup", new_token) is True


def test_will_not_sent_on_graceful_disconnect():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw, "sub")
    gw.subscribe("sub", "tenant-a/#")
    _connected(gw, "device")
    gw.set_will("device", "tenant-a/alarm", b"offline")
    gw.disconnect("device", graceful=True)
    assert gw.take_messages("sub") == []


def test_will_sent_once_on_ungraceful_disconnect():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw, "sub")
    gw.subscribe("sub", "tenant-a/#")
    _connected(gw, "device")
    gw.set_will("device", "tenant-a/alarm", b"offline")
    gw.disconnect("device", graceful=False)
    messages = gw.take_messages("sub")
    assert len(messages) == 1
    assert messages[0] == ("tenant-a/alarm", b"offline")


def test_oversized_payload_disconnects_and_is_not_forwarded():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a", max_payload=8)
    _connected(gw, "sub")
    gw.subscribe("sub", "tenant-a/#")
    token = _connected(gw, "pub")
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.publish("pub", "tenant-a/raw", b"0123456789")
    assert _code(exc) == "PAYLOAD_TOO_LARGE"
    assert gw.is_active("pub", token) is False
    assert gw.take_messages("sub") == []
    assert any(event.get("code") == "PAYLOAD_TOO_LARGE" for event in gw.audit_events())


def test_malformed_payload_is_rejected_with_audit():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw, "pub")
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.publish("pub", "tenant-a/raw", 12345)
    assert _code(exc) == "MALFORMED_PAYLOAD"
    assert any(event.get("code") == "MALFORMED_PAYLOAD" for event in gw.audit_events())


def test_inflight_window_full_queues_instead_of_dropping():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a", inflight_window=1)
    _connected(gw, "sub")
    gw.subscribe("sub", "tenant-a/#")
    _connected(gw, "pub")
    gw.publish("pub", "tenant-a/a", b"one", qos=1, packet_id=1)
    gw.publish("pub", "tenant-a/b", b"two", qos=1, packet_id=2)
    assert gw.pending_count("pub") == 1
    assert len(gw.take_messages("sub")) == 1
    gw.ack("pub", 1)
    assert gw.pending_count("pub") == 0
    assert len(gw.take_messages("sub")) == 1


def test_retained_messages_are_filtered_by_topic_permission():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw, "pub")
    gw.publish("pub", "tenant-a/firmware/v2", b"image", retain=True)
    _connected(gw, "blocked", username="blocked-user")
    gw.add_acl("blocked-user", "tenant-a/firmware/#", allow=False)
    gw.subscribe("blocked", "tenant-a/telemetry/#")
    assert gw.take_retained("blocked") == []
    _connected(gw, "allowed", username="allowed-user")
    gw.subscribe("allowed", "tenant-a/#")
    retained = gw.take_retained("allowed")
    assert len(retained) == 1
    assert retained[0][0] == "tenant-a/firmware/v2"


def test_bidirectional_bridge_is_rejected_as_loop_risk():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.add_bridge("site-link", "both")
    assert _code(exc) == "BRIDGE_LOOP"
    gw.add_bridge("site-link", "out")
    gw.add_bridge("site-link-2", "in")


def test_shared_subscription_member_released_on_disconnect():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    _connected(gw, "worker-1")
    gw.subscribe_shared("worker-1", "group-a", "tenant-a/jobs/#")
    assert gw.shared_members("group-a") == ["worker-1"]
    gw.disconnect("worker-1", graceful=True)
    assert gw.shared_members("group-a") == []


def test_tls_cn_mismatch_denies_connection():
    gw = mqtt_topic_matrix.MqttTopicMatrix(tenant_prefix="tenant-a")
    with pytest.raises(mqtt_topic_matrix.MqttError) as exc:
        gw.connect("dev-1", "device-a", tls_cn="device-b")
    assert _code(exc) == "TLS_CN_MISMATCH"
    assert any(event.get("code") == "TLS_CN_MISMATCH" for event in gw.audit_events())
    token = gw.connect("dev-1", "device-a", tls_cn="device-a")
    assert gw.is_active("dev-1", token) is True
