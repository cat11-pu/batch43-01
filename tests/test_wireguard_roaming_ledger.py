import pytest

import wireguard_roaming_ledger


def test_handshake_replay_hits_existing_session():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    first = ledger.begin_handshake("pk-a", 7, timestamp=100)
    again = ledger.begin_handshake("pk-a", 7, timestamp=101)
    assert again["session_id"] == first["session_id"]
    assert again["created"] is False


def test_handshake_replay_does_not_reset_replay_counter():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7, timestamp=100)
    assert ledger.check_replay("pk-a", 5)["accepted"] is True
    ledger.begin_handshake("pk-a", 7, timestamp=101)
    assert ledger.check_replay("pk-a", 4)["accepted"] is False


def test_bad_cookie_is_rejected_with_code():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.issue_cookie("198.51.100.4")
    with pytest.raises(wireguard_roaming_ledger.WireGuardError) as exc:
        ledger.verify_cookie("pk-a", "forged", "198.51.100.4")
    assert exc.value.code == "WG_COOKIE_INVALID"


def test_cookie_issued_for_source_is_accepted():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    cookie = ledger.issue_cookie("198.51.100.4")
    assert ledger.verify_cookie("pk-a", cookie, "198.51.100.4")["valid"] is True


def test_unauthenticated_endpoint_update_keeps_old_endpoint():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7, timestamp=100, source_address="198.51.100.4")
    ledger.complete_handshake("pk-a")
    result = ledger.update_endpoint("pk-a", "203.0.113.9", authenticated=False)
    assert result["updated"] is False
    assert result["endpoint"] == "198.51.100.4"


def test_authenticated_endpoint_update_moves_keepalive():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7, timestamp=100, source_address="198.51.100.4")
    ledger.complete_handshake("pk-a")
    result = ledger.update_endpoint("pk-a", "203.0.113.9", authenticated=True)
    assert result == {"endpoint": "203.0.113.9", "updated": True}
    assert ledger.keepalive_targets() == ["203.0.113.9"]


def test_sequence_below_highest_is_replay():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7)
    assert ledger.check_replay("pk-a", 1)["accepted"] is True
    assert ledger.check_replay("pk-a", 2)["accepted"] is True
    result = ledger.check_replay("pk-a", 1)
    assert result["accepted"] is False
    assert result["code"] == "WG_REPLAY"


def test_replay_counter_survives_restart():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7)
    ledger.check_replay("pk-a", 9)
    state = ledger.dump_state()
    restarted = wireguard_roaming_ledger.WireGuardRoamingLedger()
    restarted.load_state(state)
    result = restarted.check_replay("pk-a", 8)
    assert result["accepted"] is False
    assert result["code"] == "WG_REPLAY"


def test_transport_before_handshake_completes_is_rejected():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7)
    with pytest.raises(wireguard_roaming_ledger.WireGuardError) as exc:
        ledger.receive_transport("pk-a", 1, b"payload")
    assert exc.value.code == "WG_HANDSHAKE_INCOMPLETE"


def test_sender_index_conflict_does_not_overwrite_session():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7)
    with pytest.raises(wireguard_roaming_ledger.WireGuardError) as exc:
        ledger.begin_handshake("pk-b", 7)
    assert exc.value.code == "WG_SESSION_CONFLICT"


def test_overlapping_allowed_ips_are_rejected():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.add_allowed_ip("pk-a", "10.0.0.0/24")
    with pytest.raises(wireguard_roaming_ledger.WireGuardError) as exc:
        ledger.add_allowed_ip("pk-b", "10.0.0.128/25")
    assert exc.value.code == "WG_ALLOWED_IPS_CONFLICT"


def test_preset_psk_mismatch_fails_handshake():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger(preset_psk=b"right")
    ledger.begin_handshake("pk-a", 7)
    with pytest.raises(wireguard_roaming_ledger.WireGuardError) as exc:
        ledger.complete_handshake("pk-a", psk=b"wrong")
    assert exc.value.code == "WG_PSK_MISMATCH"


def test_delete_peer_stops_keepalive():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.begin_handshake("pk-a", 7, source_address="198.51.100.4")
    ledger.complete_handshake("pk-a")
    result = ledger.delete_peer("pk-a")
    assert result["deleted"] is True
    assert result["keepalive_active"] is False
    assert ledger.keepalive_targets() == []


def test_rekey_due_after_time_jump():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger(max_handshake_age=120)
    ledger.begin_handshake("pk-a", 7, timestamp=1000)
    assert ledger.rekey_due("pk-a", 1050) is False
    assert ledger.rekey_due("pk-a", 1120) is True
    assert ledger.rekey_due("pk-a", 999999) is True


def test_lookup_peer_routes_to_owning_key():
    ledger = wireguard_roaming_ledger.WireGuardRoamingLedger()
    ledger.add_allowed_ip("pk-a", "10.1.0.0/24")
    ledger.add_allowed_ip("pk-b", "10.2.0.0/24")
    assert ledger.lookup_peer("10.1.0.5") == "pk-a"
    assert ledger.lookup_peer("10.2.0.5") == "pk-b"
    assert ledger.lookup_peer("10.3.0.5") is None
