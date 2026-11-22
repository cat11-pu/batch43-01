import pytest

import kerberos_ticket_vault


def _ticket(**overrides):
    ticket = {
        "ticket_id": "tgt-1",
        "expires_at": 2000,
        "renew_till": 10000,
        "realm": "CORP.LOCAL",
    }
    ticket.update(overrides)
    return ticket


def _vault(**kwargs):
    vault = kerberos_ticket_vault.KerberosTicketVault(**kwargs)
    vault.store_tgt("alice", _ticket(), key_version=2)
    return vault


def test_renewing_expired_tgt_fails_with_stage():
    vault = _vault()
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.renew("alice", now=3000, client_time=3000)
    assert exc.value.code == "KRB_AP_ERR_TKT_EXPIRED"
    assert exc.value.stage == "renew"


def test_clock_skew_above_threshold_is_rejected():
    vault = _vault(clock_skew=300)
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.renew("alice", now=1000, client_time=1400)
    assert exc.value.code == "KRB_AP_ERR_SKEW"


def test_clock_skew_within_threshold_is_accepted():
    vault = _vault(clock_skew=300)
    result = vault.renew("alice", now=1000, client_time=1250)
    assert result["renewed"] is True


def test_switch_principal_clears_service_ticket_cache():
    vault = _vault()
    vault.set_service_ticket("alice", "fileserver", "svc-alice")
    assert vault.get_service_ticket("alice", "fileserver") == "svc-alice"
    vault.switch_principal("bob")
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.get_service_ticket("alice", "fileserver")
    assert exc.value.code == "KRB_NO_TICKET"


def test_keytab_version_mismatch_is_rejected():
    vault = _vault()
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.check_keytab("alice", key_version=3)
    assert exc.value.code == "KRB_AP_ERR_BADKEYVER"


def test_duplicate_authenticator_is_replay():
    vault = _vault()
    assert vault.authenticate("alice", "auth-1")["authenticated"] is True
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.authenticate("alice", "auth-1")
    assert exc.value.code == "KRB_AP_ERR_REPLAY"


def test_replay_cache_survives_restart():
    vault = _vault()
    vault.authenticate("alice", "auth-1")
    state = vault.dump_state()
    restarted = kerberos_ticket_vault.KerberosTicketVault()
    restarted.load_state(state)
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        restarted.authenticate("alice", "auth-1")
    assert exc.value.code == "KRB_AP_ERR_REPLAY"


def test_cross_realm_tgt_rejects_wrong_realm():
    vault = _vault()
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.renew_cross_realm("alice", realm="OTHER.LOCAL")
    assert exc.value.code == "KRB_AP_ERR_WRONG_REALM"


def test_non_owner_cannot_read_ticket_cache():
    vault = _vault()
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.check_cache_access(owner="alice", requester="bob")
    assert exc.value.code == "KRB_CACHE_PERMISSION"


def test_renewal_beyond_renew_till_is_rejected():
    vault = _vault()
    vault.store_tgt("alice", _ticket(renew_till=1500), key_version=2)
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.renew("alice", now=1600, client_time=1600)
    assert exc.value.code == "KRB_AP_ERR_RENEW_EXPIRED"


def test_empty_preauth_type_fails_at_preauth_stage():
    vault = _vault()
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.accept_preauth(None)
    assert exc.value.code == "KRB_AP_ERR_PREAUTH_REQUIRED"
    assert exc.value.stage == "preauth"


def test_delegation_to_unlisted_service_is_denied():
    vault = _vault()
    with pytest.raises(kerberos_ticket_vault.KerberosError) as exc:
        vault.delegate("svc-ticket", "fileserver",
                       allowed_services=("printserver",))
    assert exc.value.code == "KRB_DELEGATION_DENIED"


def test_concurrent_renewals_keep_one_tgt():
    vault = _vault()
    first = vault.renew("alice", now=1000, client_time=1000)
    second = vault.renew("alice", now=1000, client_time=1000)
    assert first["ticket_id"] == second["ticket_id"] == "tgt-1"


def test_successful_renew_returns_extended_expiry():
    vault = _vault()
    result = vault.renew("alice", now=1000, client_time=1000)
    assert result["renewed"] is True
    assert result["expires_at"] > 1000


def test_valid_preauth_type_is_accepted():
    vault = _vault()
    assert vault.accept_preauth("aes256-cts")["accepted"] is True
