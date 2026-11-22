import pytest

import ldap_bind_pool


def _handle(pool):
    handle = pool.acquire()["handle"]
    pool.connect(handle, starttls=True)
    return handle


def test_anonymous_bind_is_rejected():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    with pytest.raises(ldap_bind_pool.LdapError) as exc:
        pool.bind(handle, "", "")
    assert exc.value.code == "LDAP_ANONYMOUS_DENIED"


def test_rebind_removes_service_identity_and_roles():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    pool.bind_service(handle, "cn=svc,dc=corp,dc=local", "svc-pass")
    assert pool.has_role(handle, "admin") is True
    pool.rebind(handle, "cn=alice,ou=users", "user-pass")
    assert pool.current_identity(handle) == "cn=alice,ou=users"
    assert pool.has_role(handle, "admin") is False


def test_release_clears_identity():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    pool.bind(handle, "cn=alice,ou=users", "user-pass")
    pool.release(handle)
    new_handle = _handle(pool)
    assert pool.current_identity(new_handle) is None


def test_locked_account_fails_on_pooled_connection():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    pool.bind(handle, "cn=alice,ou=users", "user-pass")
    pool.lock_account("cn=alice,ou=users")
    with pytest.raises(ldap_bind_pool.LdapError) as exc:
        pool.rebind(handle, "cn=alice,ou=users", "user-pass")
    assert exc.value.code == "LDAP_ACCOUNT_LOCKED"


def test_referral_outside_whitelist_is_blocked():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    with pytest.raises(ldap_bind_pool.LdapError) as exc:
        pool.follow_referral(handle, "dc=evil,dc=local")
    assert exc.value.code == "LDAP_REFERRAL_BLOCKED"


def test_referral_inside_whitelist_is_allowed():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    result = pool.follow_referral(handle, "ou=people,dc=corp,dc=local")
    assert result["followed"] is True


def test_page_cookie_cannot_be_reused_across_queries():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    first = pool.search(handle, "dc=corp,dc=local", query_id="q1")
    cookie = first["cookie"]
    assert cookie
    with pytest.raises(ldap_bind_pool.LdapError) as exc:
        pool.search(handle, "dc=corp,dc=local", page_cookie=cookie,
                    query_id="q2")
    assert exc.value.code == "LDAP_COOKIE_REPLAY"


def test_starttls_missing_rejects_bind():
    pool = ldap_bind_pool.LdapBindPool(tls_required=True)
    handle = pool.acquire()["handle"]
    pool.connect(handle, starttls=False)
    with pytest.raises(ldap_bind_pool.LdapError) as exc:
        pool.bind(handle, "cn=alice,ou=users", "user-pass")
    assert exc.value.code == "LDAP_TLS_REQUIRED"


def test_sasl_downgrade_is_rejected():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    with pytest.raises(ldap_bind_pool.LdapError) as exc:
        pool.sasl_bind(handle, "SIMPLE")
    assert exc.value.code == "LDAP_SASL_DOWNGRADE"


def test_dn_is_normalized_case_and_spaces():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    pool.bind(handle, "CN=Alice, OU=Users", "user-pass")
    assert pool.current_identity(handle) == "cn=alice,ou=users"


def test_root_dse_omits_internal_attributes():
    pool = ldap_bind_pool.LdapBindPool()
    dse = pool.root_dse()
    assert isinstance(dse, dict)
    assert "internalAttrs" not in dse


def test_rebind_audit_records_previous_and_new_identity():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    pool.bind_service(handle, "cn=svc,dc=corp,dc=local", "svc-pass")
    pool.rebind(handle, "cn=alice,ou=users", "user-pass")
    entry = pool.audit_log()[-1]
    assert entry["previous"] == "cn=svc,dc=corp,dc=local"
    assert entry["new"] == "cn=alice,ou=users"


def test_two_handles_keep_separate_identities():
    pool = ldap_bind_pool.LdapBindPool()
    first = _handle(pool)
    second = _handle(pool)
    pool.bind(first, "cn=alice,ou=users", "pass-a")
    pool.bind(second, "cn=bob,ou=users", "pass-b")
    assert pool.current_identity(first) == "cn=alice,ou=users"
    assert pool.current_identity(second) == "cn=bob,ou=users"


def test_empty_password_rebind_is_not_anonymous_success():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    pool.bind(handle, "cn=alice,ou=users", "user-pass")
    with pytest.raises(ldap_bind_pool.LdapError) as exc:
        pool.rebind(handle, "cn=alice,ou=users", "")
    assert exc.value.code == "LDAP_ANONYMOUS_DENIED"


def test_acquire_after_release_has_no_service_identity():
    pool = ldap_bind_pool.LdapBindPool()
    handle = _handle(pool)
    pool.bind_service(handle, "cn=svc,dc=corp,dc=local", "svc-pass")
    pool.release(handle)
    new_handle = _handle(pool)
    pool.bind(new_handle, "cn=bob,ou=users", "pass-b")
    assert pool.current_identity(new_handle) == "cn=bob,ou=users"
    assert pool.has_role(new_handle, "admin") is False
