from concurrent.futures import ThreadPoolExecutor

import caep_event_journal


def _journal(state_path=None, **kwargs):
    return caep_event_journal.CaepEventJournal(state_path=state_path, **kwargs)


def _event(
    event_id="e1",
    event_type="session-revoked",
    session="s1",
    issuer="issuer-a",
    iat=0.0,
    exp=1000.0,
    signature_valid=True,
    subject="user-1",
    subject_issuer="issuer-a",
):
    return {
        "id": event_id,
        "types": [event_type],
        "session_id": session,
        "issuer": issuer,
        "iat": iat,
        "exp": exp,
        "signature_valid": signature_valid,
        "subject": subject,
        "subject_issuer": subject_issuer,
    }


def test_replayed_event_is_rejected_and_state_unchanged():
    journal = _journal()
    journal.ingest(_event(event_type="session-established"), now=0.0)
    journal.ingest(_event(event_id="e2"), now=1.0)
    replay = journal.ingest(_event(event_id="e2"), now=2.0)
    assert replay["accepted"] is False
    assert replay["code"] == "SET_REPLAY"
    assert journal.is_active("s1", now=2.0) is False


def test_replay_protection_survives_restart(tmp_path):
    state = str(tmp_path / "caep.json")
    journal = _journal(state)
    journal.ingest(_event(), now=0.0)
    restarted = _journal(state)
    replay = restarted.ingest(_event(), now=1.0)
    assert replay["code"] == "SET_REPLAY"
    assert restarted.is_active("s1", now=1.0) is False


def test_revoke_unknown_session_writes_tombstone():
    journal = _journal()
    result = journal.ingest(_event(session="ghost"), now=0.0)
    assert result["accepted"] is True
    assert "ghost" in journal.tombstones()
    later = journal.ingest(
        _event(event_id="e2", event_type="session-established", session="ghost"), now=1.0
    )
    assert later["code"] == "SET_TOMBSTONED"
    assert journal.is_active("ghost", now=1.0) is False


def test_tombstone_expires_and_allows_new_session():
    journal = _journal(tombstone_ttl=10.0)
    journal.ingest(_event(session="ghost"), now=0.0)
    result = journal.ingest(
        _event(event_id="e2", event_type="session-established", session="ghost"), now=11.0
    )
    assert result["accepted"] is True
    assert journal.is_active("ghost", now=11.0) is True


def test_out_of_order_establish_cannot_revive_revoked_session():
    journal = _journal()
    journal.ingest(_event(session="s9"), now=0.0)
    journal.ingest(_event(event_id="e2", event_type="session-established", session="s9"), now=1.0)
    assert journal.is_active("s9", now=1.0) is False


def test_event_with_unknown_type_rejects_whole_event():
    journal = _journal()
    journal.ingest(_event(event_type="session-established"), now=0.0)
    event = _event(event_id="e2")
    event["types"] = ["session-revoked", "unknown-type"]
    result = journal.ingest(event, now=1.0)
    assert result["accepted"] is False
    assert result["code"] == "SET_UNKNOWN_TYPE"
    assert journal.is_active("s1", now=1.0) is True
    assert len(journal.journal()) == 1


def test_issuer_mismatch_rejected():
    journal = _journal()
    result = journal.ingest(_event(issuer="other-issuer"), now=0.0)
    assert result["accepted"] is False
    assert result["code"] == "SET_ISSUER_MISMATCH"


def test_invalid_signature_rejected():
    journal = _journal()
    result = journal.ingest(_event(signature_valid=False), now=0.0)
    assert result["accepted"] is False
    assert result["code"] == "SET_SIGNATURE_INVALID"
    assert len(journal.journal()) == 0


def test_expired_event_rejected():
    journal = _journal()
    result = journal.ingest(_event(exp=5.0), now=10.0)
    assert result["accepted"] is False
    assert result["code"] == "SET_EXPIRED"


def test_multiple_event_types_are_all_applied():
    journal = _journal()
    journal.ingest(_event(event_type="session-established"), now=0.0)
    event = _event(event_id="e2")
    event["types"] = ["session-revoked", "credential-change"]
    journal.ingest(event, now=1.0)
    assert journal.is_active("s1", now=1.0) is False
    assert journal.journal()[-1]["types"] == ["session-revoked", "credential-change"]


def test_subject_issuer_mismatch_rejected():
    journal = _journal()
    result = journal.ingest(_event(subject_issuer="other-issuer"), now=0.0)
    assert result["accepted"] is False
    assert result["code"] == "SET_SUBJECT_MISMATCH"


def test_known_session_revoke_deactivates_it():
    journal = _journal()
    journal.ingest(_event(event_type="session-established"), now=0.0)
    assert journal.is_active("s1", now=0.0) is True
    journal.ingest(_event(event_id="e2"), now=1.0)
    assert journal.is_active("s1", now=1.0) is False


def test_concurrent_duplicate_ingest_writes_one_record():
    journal = _journal()
    journal.ingest(_event(event_type="session-established"), now=0.0)

    def ingest(_):
        return journal.ingest(_event(event_id="e2"), now=1.0)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(ingest, range(4)))
    accepted = [item for item in results if item["accepted"]]
    assert len(accepted) == 1
    assert len(journal.journal()) == 2


def test_session_established_event_is_journalled_and_active():
    journal = _journal()
    result = journal.ingest(_event(event_type="session-established"), now=0.0)
    assert result["accepted"] is True
    assert result["code"] is None
    assert journal.is_active("s1", now=0.0) is True
    assert journal.journal()[0]["id"] == "e1"


def test_replay_does_not_double_revocation_count():
    journal = _journal()
    journal.ingest(_event(), now=0.0)
    journal.ingest(_event(), now=1.0)
    assert journal.counters()["revocations"] == 1
    assert journal.counters()["replays"] == 1
