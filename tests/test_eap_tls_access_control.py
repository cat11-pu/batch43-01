import pytest

import eap_tls_access_control


def _cert(cn="alice", san=(), not_after=2000, usage="clientAuth",
          chain_trusted=True, root="root-fp"):
    return {
        "subject_cn": cn,
        "san": list(san),
        "not_before": 0,
        "not_after": not_after,
        "usage": usage,
        "chain_trusted": chain_trusted,
        "root_fingerprint": root,
    }


def _control(**kwargs):
    return eap_tls_access_control.EapTlsAccessControl(
        vlan_map={"alice": 10, "bob": 20, "zhangsan": 10, "finance": 30,
                  "guest": 999},
        **kwargs)


def test_expired_certificate_is_rejected_and_port_stays_down():
    control = _control()
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.authenticate("aa:01", "port-1", _cert(not_after=500), now=1000)
    assert exc.value.code == "EAP_CERT_EXPIRED"
    assert control.port_authorized("port-1") is False


def test_untrusted_chain_is_rejected():
    control = _control()
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.authenticate("aa:01", "port-1",
                             _cert(chain_trusted=False))
    assert exc.value.code == "EAP_CERT_UNTRUSTED"


def test_cn_prefix_does_not_grant_another_users_vlan():
    control = _control()
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.authenticate("aa:01", "port-1", _cert(cn="zhangsanfeng"))
    assert exc.value.code == "EAP_IDENTITY_UNKNOWN"


def test_exact_san_mapping_authorizes_vlan():
    control = _control()
    result = control.authenticate("aa:01", "port-1",
                                  _cert(cn="svc-1", san=("finance",)))
    assert result["authorized"] is True
    assert result["vlan"] == 30
    assert control.port_authorized("port-1") is True


def test_reauth_revokes_previous_session():
    control = _control()
    first = control.authenticate("aa:01", "port-1", _cert())
    second = control.reauth("aa:01", "port-1", _cert())
    assert second["session_id"] != first["session_id"]
    assert control.session_active(first["session_id"]) is False
    assert control.session_active(second["session_id"]) is True


def test_same_mac_on_new_port_revokes_old_port():
    control = _control()
    control.authenticate("aa:01", "port-1", _cert())
    control.authenticate("aa:01", "port-2", _cert())
    assert control.port_authorized("port-1") is False
    assert control.port_authorized("port-2") is True


def test_accounting_has_one_start_and_one_stop():
    control = _control()
    session = control.authenticate("aa:01", "port-1", _cert())
    control.accounting_start(session["session_id"])
    control.accounting_start(session["session_id"])
    control.accounting_stop(session["session_id"])
    control.accounting_stop(session["session_id"])
    assert control.accounting_events(session["session_id"]) == \
        {"starts": 1, "stops": 1}


def test_failure_threshold_locks_account_and_raises_alarm():
    control = _control(failure_threshold=5)
    for _ in range(5):
        with pytest.raises(eap_tls_access_control.EapError):
            control.authenticate("aa:01", "port-1", _cert(not_after=500),
                                 now=1000)
    assert control.is_locked("aa:01") is True
    assert "EAP_LOCKED" in control.alarms()


def test_locked_mac_is_rejected_even_with_valid_certificate():
    control = _control(failure_threshold=2)
    for _ in range(2):
        with pytest.raises(eap_tls_access_control.EapError):
            control.authenticate("aa:01", "port-1", _cert(not_after=500),
                                 now=1000)
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.authenticate("aa:01", "port-1", _cert())
    assert exc.value.code == "EAP_LOCKED"
    assert control.port_authorized("port-1") is False


def test_restore_does_not_trust_old_port_authorizations():
    control = _control()
    result = control.restore_state({"authorized_ports": ["port-1", "port-2"]})
    assert result["authorized_ports"] == 0
    assert control.port_authorized("port-1") is False


def test_eap_success_before_identity_is_rejected():
    control = _control()
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.eap_success("aa:01")
    assert exc.value.code == "EAP_PREMATURE_SUCCESS"


def test_mab_outside_whitelist_is_denied():
    control = _control()
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.mab_authenticate("de:ad:be:ef:00:01", "port-3",
                                 whitelist=("aa:01",))
    assert exc.value.code == "EAP_MAB_DENIED"


def test_root_certificate_change_raises_alarm():
    control = _control()
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.authenticate("aa:01", "port-1", _cert(root="other-root"))
    assert exc.value.code == "EAP_ROOT_CHANGED"
    assert "EAP_ROOT_CHANGED" in control.alarms()


def test_guest_vlan_cannot_reach_server_vlan():
    control = _control()
    control.authenticate("aa:01", "port-1", _cert(cn="guest"))
    assert control.can_access("aa:01", 10) is False
    assert control.can_access("aa:01", 999) is True


def test_fragment_declared_length_over_limit_is_rejected():
    control = _control(max_eap_len=65535)
    with pytest.raises(eap_tls_access_control.EapError) as exc:
        control.reassemble_fragments([b"aa", b"bb"], declared_len=70000)
    assert exc.value.code == "EAP_FRAGMENT_LENGTH_MISMATCH"
