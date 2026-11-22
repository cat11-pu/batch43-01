import pytest

import oidc_logout_mirror

CALLBACK = "https://app1.example.com/cb"


def _mirror_with_app():
    mirror = oidc_logout_mirror.OidcLogoutMirror()
    mirror.register_callback("app1", CALLBACK)
    return mirror


def test_repeated_logout_returns_same_token_once():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    first = mirror.logout(session)
    second = mirror.logout(session)
    assert first["status"] == "logged_out"
    assert second["status"] == "already_logged_out"
    assert second["token"] == first["token"]
    assert mirror.is_active(session) is False


def test_logout_does_not_touch_other_sessions_of_same_user():
    mirror = _mirror_with_app()
    first = mirror.create_session("alice", "phone")["session_id"]
    second = mirror.create_session("alice", "laptop")["session_id"]
    mirror.logout(first)
    assert mirror.is_active(first) is False
    assert mirror.is_active(second) is True


def test_callback_prefix_lookalike_is_rejected():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    with pytest.raises(oidc_logout_mirror.OidcError) as exc:
        mirror.deliver(token, "app1",
                       callback_url="https://app1.example.com.evil.com/cb")
    assert exc.value.code == "OIDC_CALLBACK_MISMATCH"


def test_registered_callback_is_delivered():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    result = mirror.deliver(token, "app1", callback_url=CALLBACK)
    assert result["state"] == "delivered"
    assert mirror.delivery_state(token, "app1")["state"] == "delivered"


def test_failed_delivery_enters_retry_queue():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    result = mirror.deliver(token, "app1", callback_url=CALLBACK,
                            delivered=False)
    assert result["state"] == "pending"
    assert len(mirror.retry_queue()) == 1


def test_three_failures_mark_delivery_dead():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    mirror.deliver(token, "app1", callback_url=CALLBACK, delivered=False)
    mirror.attempt_delivery(token, "app1", success=False)
    mirror.attempt_delivery(token, "app1", success=False)
    state = mirror.delivery_state(token, "app1")
    assert state == {"state": "dead", "attempts": 3}
    assert mirror.retry_queue() == []


def test_dead_delivery_is_not_retried():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    mirror.deliver(token, "app1", callback_url=CALLBACK, delivered=False)
    for _ in range(3):
        mirror.attempt_delivery(token, "app1", success=False)
    result = mirror.attempt_delivery(token, "app1", success=True)
    assert result["state"] == "dead"
    assert result["attempts"] == 3


def test_refresh_after_logout_is_invalid():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    refresh_token = mirror.issue_refresh_token(session)["refresh_token"]
    mirror.logout(session)
    with pytest.raises(oidc_logout_mirror.OidcError) as exc:
        mirror.refresh(refresh_token)
    assert exc.value.code == "OIDC_INVALID_SESSION"


def test_password_change_invalidates_refresh_token():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    refresh_token = mirror.issue_refresh_token(session)["refresh_token"]
    result = mirror.change_password("alice")
    assert result["invalidated_sessions"] == 1
    assert mirror.is_active(session) is False
    with pytest.raises(oidc_logout_mirror.OidcError) as exc:
        mirror.refresh(refresh_token)
    assert exc.value.code == "OIDC_INVALID_SESSION"


def test_blocked_front_channel_stays_pending():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    result = mirror.record_front_channel(token, "app1", delivered=False)
    assert result["state"] == "pending"
    assert mirror.delivery_state(token, "app1")["state"] == "pending"
    assert len(mirror.retry_queue()) == 1


def test_restart_resumes_pending_delivery():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    mirror.deliver(token, "app1", callback_url=CALLBACK, delivered=False)
    state = mirror.dump_state()
    restarted = oidc_logout_mirror.OidcLogoutMirror()
    restarted.load_state(state)
    assert len(restarted.retry_queue()) == 1
    assert restarted.delivery_state(token, "app1")["state"] == "pending"


def test_token_future_time_beyond_skew_is_rejected():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session, now=300)["token"]
    with pytest.raises(oidc_logout_mirror.OidcError) as exc:
        mirror.validate_token(token, now=0, skew=60)
    assert exc.value.code == "OIDC_TOKEN_SKEW"
    assert mirror.validate_token(token, now=280, skew=60)["valid"] is True


def test_refresh_token_is_single_use():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    refresh_token = mirror.issue_refresh_token(session)["refresh_token"]
    first = mirror.refresh(refresh_token)
    assert first["session_id"] == session
    with pytest.raises(oidc_logout_mirror.OidcError) as exc:
        mirror.refresh(refresh_token)
    assert exc.value.code == "OIDC_INVALID_SESSION"


def test_refresh_token_is_bound_to_one_session():
    mirror = _mirror_with_app()
    first = mirror.create_session("alice", "phone")["session_id"]
    second = mirror.create_session("alice", "laptop")["session_id"]
    first_token = mirror.issue_refresh_token(first)["refresh_token"]
    second_token = mirror.issue_refresh_token(second)["refresh_token"]
    assert mirror.refresh(first_token)["session_id"] == first
    assert mirror.is_active(second) is True
    assert mirror.refresh(second_token)["session_id"] == second


def test_repeated_successful_delivery_is_idempotent():
    mirror = _mirror_with_app()
    session = mirror.create_session("alice", "phone")["session_id"]
    token = mirror.logout(session)["token"]
    first = mirror.deliver(token, "app1", callback_url=CALLBACK)
    second = mirror.deliver(token, "app1", callback_url=CALLBACK)
    assert first["attempts"] == 1
    assert second["attempts"] == 1
    assert second["state"] == "delivered"
    assert mirror.retry_queue() == []
