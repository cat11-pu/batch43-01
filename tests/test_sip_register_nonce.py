import pytest

import sip_register_nonce


def _auth(nonce, response="digest-value", stale=False):
    return {"nonce": nonce, "response": response, "stale": stale}


def _contacts(*items):
    return [{"uri": uri, "q": q} for uri, q in items]


def test_reused_nonce_returns_401_and_keeps_binding():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    first = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                            3600, 5, _auth(nonce), now=10)
    assert first["status"] == 200
    again = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                            3600, 6, _auth(nonce), now=20)
    assert again["status"] == 401
    assert again["challenge"] is not None
    assert len(again["bindings"]) == 1


def test_expired_nonce_does_not_delete_existing_binding():
    server = sip_register_nonce.SipRegisterServer()
    first_nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 3600, 5,
                    _auth(first_nonce), now=10)
    old_nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                             3600, 6, _auth(old_nonce), now=5000)
    assert result["status"] == 401
    assert len(result["bindings"]) == 1


def test_expires_zero_removes_only_matching_contact():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1",
                    _contacts(("sip:a@x", 0.5), ("sip:b@x", 0.4)),
                    3600, 5, _auth(nonce), now=10)
    remove_nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                             0, 6, _auth(remove_nonce), now=20)
    assert result["status"] == 200
    assert [b["contact"] for b in result["bindings"]] == ["sip:b@x"]


def test_expires_zero_for_unknown_contact_keeps_existing():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 3600, 5,
                    _auth(nonce), now=10)
    remove_nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1", _contacts(("sip:other@x", 0.5)),
                             0, 6, _auth(remove_nonce), now=20)
    assert result["status"] == 200
    assert [b["contact"] for b in result["bindings"]] == ["sip:a@x"]


def test_replayed_cseq_is_not_a_new_registration():
    server = sip_register_nonce.SipRegisterServer()
    first_nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 3600, 5,
                    _auth(first_nonce), now=10)
    replay_nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1",
                             _contacts(("sip:new@x", 0.9)), 3600, 5,
                             _auth(replay_nonce), now=20)
    assert result["replay"] is True
    assert len(result["bindings"]) == 1
    assert result["bindings"][0]["contact"] == "sip:a@x"


def test_repeated_registration_keeps_single_binding():
    server = sip_register_nonce.SipRegisterServer()
    first_nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 3600, 5,
                    _auth(first_nonce), now=10)
    second_nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                             3600, 6, _auth(second_nonce), now=20)
    assert result["status"] == 200
    assert len(result["bindings"]) == 1


def test_highest_q_value_is_primary():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1",
                             _contacts(("sip:low@x", 0.2), ("sip:high@x", 0.9)),
                             3600, 5, _auth(nonce), now=10)
    assert result["primary_contact"] == "sip:high@x"


def test_nat_received_and_rport_are_used_for_routing():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@10.0.0.5", 0.5)),
                    3600, 5, _auth(nonce), now=10,
                    received="198.51.100.9", rport=5062)
    assert server.routing_target("dev-1", "call-1", "sip:a@10.0.0.5") == \
        "198.51.100.9:5062"


def test_authorization_without_response_is_rejected():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                             3600, 5, _auth(nonce, response=None), now=10)
    assert result["status"] == 401
    assert result["bindings"] == []


def test_transport_disconnect_removes_binding():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 3600, 5,
                    _auth(nonce), now=10)
    assert server.disconnect("dev-1", "call-1")["removed"] == 1
    assert server.bindings("dev-1") == []


def test_challenges_are_unique():
    server = sip_register_nonce.SipRegisterServer()
    first = server.challenge("dev-1", now=0)["nonce"]
    second = server.challenge("dev-1", now=0)["nonce"]
    assert first != second


def test_clock_skew_tolerance_accepts_old_nonce():
    server = sip_register_nonce.SipRegisterServer(nonce_ttl=300, clock_skew=900)
    nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                             3600, 5, _auth(nonce), now=600)
    assert result["status"] == 200


def test_binding_expires_at_expires_time():
    server = sip_register_nonce.SipRegisterServer()
    nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 60, 5,
                    _auth(nonce), now=100)
    assert server.expire(now=161)["expired"] == 1
    assert server.bindings("dev-1") == []


def test_stale_flag_forces_new_challenge_without_touching_bindings():
    server = sip_register_nonce.SipRegisterServer()
    first_nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 3600, 5,
                    _auth(first_nonce), now=10)
    stale_nonce = server.challenge("dev-1", now=0)["nonce"]
    result = server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)),
                             3600, 6, _auth(stale_nonce, stale=True), now=20)
    assert result["status"] == 401
    assert result["challenge"] is not None
    assert len(result["bindings"]) == 1


def test_devices_keep_independent_bindings():
    server = sip_register_nonce.SipRegisterServer()
    first_nonce = server.challenge("dev-1", now=0)["nonce"]
    server.register("dev-1", "call-1", _contacts(("sip:a@x", 0.5)), 3600, 5,
                    _auth(first_nonce), now=10)
    second_nonce = server.challenge("dev-2", now=0)["nonce"]
    server.register("dev-2", "call-2", _contacts(("sip:b@x", 0.5)), 3600, 5,
                    _auth(second_nonce), now=10)
    server.disconnect("dev-1", "call-1")
    assert server.bindings("dev-1") == []
    assert len(server.bindings("dev-2")) == 1
