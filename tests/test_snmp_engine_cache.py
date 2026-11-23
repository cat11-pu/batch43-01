import pytest

import snmp_engine_cache


def _discover(cache, engine_id="eng-1", user="alice", engine_time=1000,
              context=""):
    return cache.discover(
        engine_id, 5, engine_time, user, "198.51.100.10", context=context)


def _request(**overrides):
    request = {
        "engine_id": "eng-1",
        "user": "alice",
        "auth_protocol": "SHA-256",
        "auth_ok": True,
        "auth_key": b"auth-material",
        "privacy_key": b"priv-material",
        "engine_time": 1000,
        "request_id": 11,
        "context": "",
        "data": {"ifInOctets": 42},
    }
    request.update(overrides)
    return request


def test_request_outside_time_window_has_no_data():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    with pytest.raises(snmp_engine_cache.SnmpError) as exc:
        cache.check_request(_request(engine_time=1200))
    assert exc.value.code == "usmStatsNotInTimeWindows"
    assert exc.value.data is None


def test_request_at_time_window_boundary_is_accepted():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    result = cache.check_request(_request(engine_time=1150, request_id=12))
    assert result["ok"] is True
    assert result["data"] == {"ifInOctets": 42}


def test_duplicate_request_id_returns_cached_response():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    first = cache.check_request(_request())
    second = cache.check_request(_request())
    assert second["request_id"] == first["request_id"]
    assert second["data"] == first["data"]


def test_duplicate_request_id_is_processed_once():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    cache.check_request(_request())
    cache.check_request(_request())
    stats = cache.stats()
    assert stats["processed"] == 1
    assert stats["cached_hits"] == 1


def test_unknown_user_and_bad_auth_share_same_error():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    with pytest.raises(snmp_engine_cache.SnmpError) as unknown:
        cache.check_request(_request(user="mallory"))
    with pytest.raises(snmp_engine_cache.SnmpError) as bad_auth:
        cache.check_request(_request(auth_ok=False))
    assert unknown.value.code == "usmStatsAuthFailure"
    assert bad_auth.value.code == "usmStatsAuthFailure"


def test_configured_auth_protocol_cannot_downgrade():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    with pytest.raises(snmp_engine_cache.SnmpError) as exc:
        cache.check_request(_request(auth_protocol="SHA-1"))
    assert exc.value.code == "usmStatsWrongDigests"


def test_reused_auth_and_privacy_key_is_rejected():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    with pytest.raises(snmp_engine_cache.SnmpError) as exc:
        cache.check_request(_request(privacy_key=b"auth-material"))
    assert exc.value.code == "SNMP_KEY_REUSE"


def test_unauthenticated_report_is_ignored():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    result = cache.handle_report({"engine_id": "eng-1"}, authenticated=False)
    assert result["ignored"] is True


def test_context_mismatch_is_rejected():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache, context="ctx-a")
    with pytest.raises(snmp_engine_cache.SnmpError) as exc:
        cache.check_request(_request(context="ctx-b"))
    assert exc.value.code == "SNMP_CONTEXT_MISMATCH"


def test_same_engine_user_refreshes_single_entry():
    cache = snmp_engine_cache.SnmpEngineCache()
    first = _discover(cache, engine_time=1000)
    second = cache.discover("eng-1", 6, 1300, "alice", "198.51.100.10")
    assert second["created"] is False
    assert second["cache_key"] == first["cache_key"]
    assert second["cache_size"] == 1
    assert cache.get_engine("eng-1", "alice")["engine_time"] == 1300


def test_same_engine_different_user_gets_own_entry():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache, user="alice")
    _discover(cache, user="bob")
    assert cache.get_engine("eng-1", "alice")["user"] == "alice"
    assert cache.get_engine("eng-1", "bob")["user"] == "bob"


def test_engine_cache_limit_is_enforced():
    cache = snmp_engine_cache.SnmpEngineCache(max_engines=2)
    _discover(cache, engine_id="eng-1")
    _discover(cache, engine_id="eng-2")
    with pytest.raises(snmp_engine_cache.SnmpError) as exc:
        _discover(cache, engine_id="eng-3")
    assert exc.value.code == "SNMP_ENGINE_CACHE_FULL"


def test_state_restore_keeps_boots_and_time():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache, engine_time=4321)
    state = cache.dump_state()
    restarted = snmp_engine_cache.SnmpEngineCache()
    restarted.load_state(state)
    assert restarted.get_engine("eng-1", "alice")["engine_boots"] == 5
    assert restarted.get_engine("eng-1", "alice")["engine_time"] == 4321


def test_trap_from_unknown_engine_is_rejected():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    with pytest.raises(snmp_engine_cache.SnmpError) as exc:
        cache.accept_trap({"engine_id": "eng-unknown", "event": "linkDown"})
    assert exc.value.code == "SNMP_UNKNOWN_ENGINE"


def test_valid_request_returns_data():
    cache = snmp_engine_cache.SnmpEngineCache()
    _discover(cache)
    result = cache.check_request(_request())
    assert result["ok"] is True
    assert result["request_id"] == 11
    assert result["data"] == {"ifInOctets": 42}
