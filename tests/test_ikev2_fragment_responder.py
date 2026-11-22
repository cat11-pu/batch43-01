import pytest

import ikev2_fragment_responder


def test_auth_fragment_gap_reports_fragment_incomplete():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.receive_auth_fragment("i1", "r1", 0, 3, b"aa")
    with pytest.raises(ikev2_fragment_responder.Ikev2Error) as exc:
        responder.receive_auth_fragment("i1", "r1", 2, 3, b"cc")
    assert exc.value.code == "IKE_FRAGMENT_INCOMPLETE"


def test_auth_fragments_are_assembled_in_order():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    assert responder.receive_auth_fragment("i1", "r1", 0, 2, b"ab")["status"] == "buffered"
    done = responder.receive_auth_fragment("i1", "r1", 1, 2, b"cd")
    assert done["status"] == "complete"
    assert done["payload"] == b"abcd"


def test_duplicate_auth_fragment_is_not_counted_twice():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.receive_auth_fragment("i1", "r1", 0, 2, b"ab")
    again = responder.receive_auth_fragment("i1", "r1", 0, 2, b"ab")
    assert again["status"] == "duplicate"
    assert again["received"] == 1
    assert again["total"] == 2


def test_retransmitted_ike_auth_reuses_existing_sa():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    first = responder.establish_sa("i1", "r1", b"n", 14)
    second = responder.establish_sa("i1", "r1", b"n", 14)
    assert second["reused"] is True
    assert second["sa_id"] == first["sa_id"]
    assert second["sa_count"] == 1


def test_mobike_update_without_authentication_is_rejected():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14, peer_address="198.51.100.1")
    with pytest.raises(ikev2_fragment_responder.Ikev2Error) as exc:
        responder.handle_mobike_update("i1", "r1", "198.51.100.9", authenticated=False)
    assert exc.value.code == "IKE_MOBIKE_UNAUTH"
    assert responder.peer_address("i1") == "198.51.100.1"


def test_mobike_update_from_authenticated_message_moves_peer():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14, peer_address="198.51.100.1")
    result = responder.handle_mobike_update("i1", "r1", "198.51.100.9", authenticated=True)
    assert result["peer_address"] == "198.51.100.9"
    assert responder.peer_address("i1") == "198.51.100.9"


def test_nat_detected_keepalive_uses_port_4500():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14)
    assert responder.detect_nat("i1")["nat_detected"] is True
    assert responder.send_keepalive("i1")["destination_port"] == 4500


def test_out_of_order_message_ids_are_not_treated_as_replay():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14)
    assert responder.process_ike_message("i1", 5)["status"] == "ok"
    assert responder.process_ike_message("i1", 3)["status"] == "ok"


def test_duplicate_message_id_is_replay():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14)
    responder.process_ike_message("i1", 5)
    assert responder.process_ike_message("i1", 5)["status"] == "replay"


def test_unsupported_dh_group_is_rejected_at_negotiation():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder(
        supported_dh_groups=(19, 20))
    with pytest.raises(ikev2_fragment_responder.Ikev2Error) as exc:
        responder.establish_sa("i1", "r1", b"n", 14)
    assert exc.value.code == "IKE_DH_GROUP_UNSUPPORTED"


def test_cookie_challenge_is_echoed_and_missing_cookie_rejected():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14)
    result = responder.process_notify("i1", {"type": "COOKIE", "cookie": "c-77"})
    assert result["cookie_echo"] == "c-77"
    with pytest.raises(ikev2_fragment_responder.Ikev2Error) as exc:
        responder.process_notify("i1", {"type": "COOKIE"})
    assert exc.value.code == "IKE_COOKIE_REQUIRED"


def test_child_rekey_switches_new_packets_to_new_key_only():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14)
    responder.add_child_sa("i1", "child-1")
    responder.rekey_child_sa("i1", "key-new")
    assert responder.decrypt_packet("i1", "key-new")["accepted"] is True
    assert responder.decrypt_packet("i1", "key-old")["accepted"] is False


def test_old_child_sa_is_removed_only_after_drain():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14)
    responder.add_child_sa("i1", "child-1")
    responder.rekey_child_sa("i1", "key-new")
    drained = responder.drain_old_sa("i1")
    assert drained["removed"] is True
    assert drained["remaining_sas"] == 0
    assert responder.decrypt_packet("i1", "key-old")["accepted"] is False


def test_delete_ike_sa_cascades_to_child_sas():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    responder.establish_sa("i1", "r1", b"n", 14)
    responder.add_child_sa("i1", "child-1")
    responder.add_child_sa("i1", "child-2")
    result = responder.delete_ike_sa("i1")
    assert result["deleted_child_sas"] == 2
    assert result["residual_tunnels"] == 0


def test_same_nat_source_with_multiple_peers_does_not_cross_sas():
    responder = ikev2_fragment_responder.Ikev2FragmentResponder()
    first = responder.establish_sa("i1", "r1", b"n", 14, peer_address="203.0.113.7")
    second = responder.establish_sa("i2", "r2", b"n", 14, peer_address="203.0.113.7")
    assert first["sa_id"] != second["sa_id"]
    found = responder.lookup_sa("i2", "r2", "203.0.113.7")
    assert found["sa_id"] == second["sa_id"]
    assert responder.lookup_sa("i9", "r9", "203.0.113.7") is None
