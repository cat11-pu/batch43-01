from concurrent.futures import ThreadPoolExecutor
import json

import pytest

import par_request_uri_store


def _store(state_path=None, **kwargs):
    return par_request_uri_store.ParRequestUriStore(state_path=state_path, **kwargs)


def _request(**overrides):
    request = {
        "client_id": "client-1",
        "request_client_id": "client-1",
        "scope": "openid profile",
        "requested_scope": "openid profile",
        "redirect_uri": "https://app.example/cb",
        "code_challenge_method": "S256",
        "ttl": 90.0,
    }
    request.update(overrides)
    return request


def test_unsigned_pushed_request_is_rejected():
    store = _store()
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.push(_request(), signature_valid=False)
    assert exc.value.code == "invalid_request"


def test_inner_and_outer_client_identifiers_must_match():
    store = _store()
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.push(_request(request_client_id="client-2"))
    assert exc.value.code == "invalid_client"


def test_request_uri_is_bound_to_its_client():
    store = _store()
    uri = store.push(_request())
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.authorize(uri, "client-2", redirect_uri="https://app.example/cb")
    assert exc.value.code == "invalid_client"


def test_expired_request_uri_returns_invalid_request_uri():
    store = _store()
    uri = store.push(_request(ttl=10.0))
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.authorize(uri, "client-1", redirect_uri="https://app.example/cb", now=20.0)
    assert exc.value.code == "invalid_request_uri"


def test_consumed_request_uri_cannot_be_reused():
    store = _store()
    uri = store.push(_request())
    first = store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")
    assert first["code"]
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")
    assert exc.value.code == "invalid_request_uri"


def test_concurrent_authorize_issues_exactly_one_code():
    store = _store()
    uri = store.push(_request())

    def authorize(_):
        try:
            return store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")["code"]
        except par_request_uri_store.ParError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(authorize, range(5)))
    successes = [item for item in results if item and item != "invalid_request_uri"]
    assert len(successes) == 1
    assert results.count("invalid_request_uri") == 4


def test_redirect_uri_requires_exact_match_not_prefix():
    store = _store()
    uri = store.push(_request())
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.authorize(uri, "client-1", redirect_uri="https://app.example/cb.evil")
    assert exc.value.code == "invalid_request"


def test_plain_pkce_method_is_rejected():
    store = _store()
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.push(_request(code_challenge_method="plain"))
    assert exc.value.code == "invalid_request"


def test_scope_mismatch_is_rejected_not_unioned():
    store = _store()
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.push(_request(scope="openid", requested_scope="openid admin"))
    assert exc.value.code == "invalid_request"


def test_exchange_with_plain_method_is_rejected():
    store = _store()
    uri = store.push(_request())
    code = store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")["code"]
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.exchange(code, "client-1", method="plain")
    assert exc.value.code == "invalid_request"


def test_pushed_record_survives_restart(tmp_path):
    state = str(tmp_path / "par.json")
    store = _store(state_path=state)
    uri = store.push(_request())
    restarted = _store(state_path=state)
    result = restarted.authorize(uri, "client-1", redirect_uri="https://app.example/cb")
    assert result["code"]


def test_pending_record_is_consumed_after_authorization():
    store = _store()
    uri = store.push(_request())
    assert store.pending_count() == 1
    store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")
    assert store.pending_count() == 0


def test_valid_flow_returns_code_and_access_token():
    store = _store()
    uri = store.push(_request())
    authorization = store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")
    assert authorization["code"].startswith("code-")
    token = store.exchange(authorization["code"], "client-1", method="S256")
    assert token["access_token"].startswith("at-")


def test_exchange_by_another_client_is_invalid_grant():
    store = _store()
    uri = store.push(_request())
    code = store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")["code"]
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.exchange(code, "client-2", method="S256")
    assert exc.value.code == "invalid_grant"


def test_unknown_request_uri_returns_invalid_request_uri():
    store = _store()
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.authorize("urn:ietf:params:oauth:request_uri:missing", "client-1")
    assert exc.value.code == "invalid_request_uri"


def test_consumed_state_survives_restart(tmp_path):
    state = str(tmp_path / "par.json")
    store = _store(state_path=state)
    uri = store.push(_request())
    code = store.authorize(uri, "client-1", redirect_uri="https://app.example/cb")["code"]

    restarted = _store(state_path=state)
    assert restarted.pending_count() == 0
    with pytest.raises(par_request_uri_store.ParError) as exc:
        restarted.authorize(uri, "client-1", redirect_uri="https://app.example/cb")
    assert exc.value.code == "invalid_request_uri"
    assert restarted.exchange(code, "client-1", method="S256")["access_token"].startswith("at-")


def test_repeated_parameters_are_rejected():
    store = _store()
    request = list(_request().items())
    request.append(("client_id", "client-2"))
    with pytest.raises(par_request_uri_store.ParError) as exc:
        store.push(request)
    assert exc.value.code == "invalid_request"


def test_push_history_and_audit_are_persisted(tmp_path):
    state = str(tmp_path / "par.json")
    store = _store(state_path=state)
    uri = store.push(_request())
    with pytest.raises(par_request_uri_store.ParError):
        store.push(_request(), signature_valid=False)

    restarted = _store(state_path=state)
    records = restarted.pushed_records()
    assert uri in records
    assert records[uri]["pushed_request"]["client_id"] == "client-1"
    assert [entry["result"] for entry in restarted.audit_records()] == [
        "accepted",
        "rejected",
    ]

    with open(state, "r", encoding="utf-8") as state_file:
        persisted = json.load(state_file)
    assert uri in persisted["request_uris"]
    assert len(persisted["audit"]) == 2


def test_concurrent_authorize_across_store_instances_issues_one_code(tmp_path):
    state = str(tmp_path / "par.json")
    uri = _store(state_path=state).push(_request())

    def authorize(_):
        try:
            return _store(state_path=state).authorize(
                uri, "client-1", redirect_uri="https://app.example/cb"
            )["code"]
        except par_request_uri_store.ParError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(authorize, range(6)))

    successes = [item for item in results if item and item != "invalid_request_uri"]
    assert len(successes) == 1
    assert results.count("invalid_request_uri") == 5
