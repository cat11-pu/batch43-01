from concurrent.futures import ThreadPoolExecutor

import pytest

import ciba_consent_transcript


def _transcript(state_path=None, **kwargs):
    return ciba_consent_transcript.CibaConsentTranscript(
        state_path=state_path, **kwargs
    )


def test_poll_and_callback_concurrently_issue_one_token():
    transcript = _transcript()
    transcript.start("req-1", "client-1")
    transcript.approve("req-1")

    def deliver(kind):
        try:
            if kind == "poll":
                return transcript.poll("req-1", "client-1", now=10.0).get("access_token")
            return transcript.callback("req-1", "client-1", now=10.0).get("access_token")
        except ciba_consent_transcript.CibaError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(deliver, ["poll", "callback", "poll", "callback"]))
    tokens = [item for item in results if item and not item.startswith("invalid")]
    assert len(tokens) == 1
    assert results.count("invalid_grant") == 3


def test_poll_interval_too_short_returns_slow_down():
    transcript = _transcript(poll_interval=5.0)
    transcript.start("req-2", "client-1")
    first = transcript.poll("req-2", "client-1", now=0.0)
    assert first["status"] == "authorization_pending"
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.poll("req-2", "client-1", now=1.0)
    assert exc.value.code == "slow_down"


def test_poll_from_other_client_is_unauthorized():
    transcript = _transcript()
    transcript.start("req-3", "client-1")
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.poll("req-3", "client-2")
    assert exc.value.code == "unauthorized_client"


def test_request_id_cannot_be_reused_by_another_client():
    transcript = _transcript()
    transcript.start("req-4", "client-1")
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.start("req-4", "client-2")
    assert exc.value.code == "invalid_request"


def test_expired_request_cannot_issue_token():
    transcript = _transcript(request_ttl=10.0)
    transcript.start("req-5", "client-1", now=0.0)
    transcript.approve("req-5", now=1.0)
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.poll("req-5", "client-1", now=20.0)
    assert exc.value.code == "invalid_grant"


def test_poll_before_user_approval_returns_no_token():
    transcript = _transcript()
    transcript.start("req-6", "client-1")
    result = transcript.poll("req-6", "client-1", now=0.0)
    assert result["status"] == "authorization_pending"
    assert "access_token" not in result


def test_concurrent_approvals_create_one_record():
    transcript = _transcript()
    transcript.start("req-7", "client-1")

    def approve(_):
        return transcript.approve("req-7", user="alice")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(approve, range(4)))
    assert len(transcript.records()) == 1


def test_callback_from_unverified_source_is_rejected():
    transcript = _transcript()
    transcript.start("req-8", "client-1")
    transcript.approve("req-8")
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.callback("req-8", "client-1", source_verified=False)
    assert exc.value.code == "unauthorized_client"


def test_denied_request_poll_returns_access_denied():
    transcript = _transcript()
    transcript.start("req-9", "client-1")
    transcript.deny("req-9")
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.poll("req-9", "client-1", now=10.0)
    assert exc.value.code == "access_denied"


def test_restart_delivers_approved_request_once(tmp_path):
    state = str(tmp_path / "ciba.json")
    transcript = _transcript(state)
    transcript.start("req-10", "client-1", now=0.0)
    transcript.approve("req-10", now=1.0)
    restarted = _transcript(state)
    first = restarted.poll("req-10", "client-1", now=10.0)
    assert first["access_token"]
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        restarted.poll("req-10", "client-1", now=20.0)
    assert exc.value.code == "invalid_grant"


def test_denied_request_cannot_jump_to_approved():
    transcript = _transcript()
    transcript.start("req-11", "client-1")
    transcript.deny("req-11")
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.approve("req-11")
    assert exc.value.code == "invalid_grant"


def test_approved_request_issues_token_through_poll():
    transcript = _transcript()
    transcript.start("req-12", "client-1")
    transcript.approve("req-12")
    result = transcript.poll("req-12", "client-1", now=10.0)
    assert result["access_token"].startswith("token-")
    assert result["status"] == "approved"


def test_second_token_request_returns_invalid_grant():
    transcript = _transcript()
    transcript.start("req-13", "client-1")
    transcript.approve("req-13")
    transcript.poll("req-13", "client-1", now=10.0)
    with pytest.raises(ciba_consent_transcript.CibaError) as exc:
        transcript.callback("req-13", "client-1", now=20.0)
    assert exc.value.code == "invalid_grant"


def test_callback_before_approval_does_not_issue_token():
    transcript = _transcript()
    transcript.start("req-14", "client-1")
    result = transcript.callback("req-14", "client-1", now=0.0)
    assert result["status"] == "authorization_pending"
    assert "access_token" not in result


def test_callback_after_approval_issues_token():
    transcript = _transcript()
    transcript.start("req-15", "client-1")
    transcript.approve("req-15")
    result = transcript.callback("req-15", "client-1", now=10.0)
    assert result["access_token"].startswith("token-")
