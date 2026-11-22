import pytest

import report_abuse_tribunal as rat


def make(**kwargs):
    return rat.ReportAbuseTribunal(**kwargs)


def filed(p, rid="r1", reporter="player-1", target="griefer-1", reason="abuse",
          now=0, **kwargs):
    return p.file_report(rid, reporter, target, reason, now=now, **kwargs)


def test_reports_inside_window_merge_into_one_ticket():
    p = make(merge_window=300)
    first = filed(p, "r1", "player-1", now=0)
    second = filed(p, "r2", "player-2", now=10)
    third = filed(p, "r3", "player-3", now=20)
    assert second["ticket_id"] == first["ticket_id"]
    assert third["ticket_id"] == first["ticket_id"]
    assert len(p.tickets()) == 1
    assert p.ticket(first["ticket_id"])["report_count"] == 3


def test_reports_outside_window_open_a_new_ticket():
    p = make(merge_window=300)
    first = filed(p, "r1", now=0)
    late = filed(p, "r2", "player-2", now=301)
    assert late["ticket_id"] != first["ticket_id"]
    assert len(p.tickets()) == 2


def test_evidence_is_append_only_and_keeps_first_entry():
    p = make()
    filed(p, "r1", evidence=["first line", "second line"])
    tid = p.tickets()[0]["ticket_id"]
    before = p.evidence(tid)
    filed(p, "r2", "player-2", now=5, evidence=["third line"])
    after = p.evidence(tid)
    assert len(before) == 2
    assert len(after) == 3
    assert after[0]["text"] == "first line"
    assert after[1]["text"] == "second line"


def test_voice_report_keeps_audio_evidence():
    p = make()
    filed(p, "r1", evidence=["voice note"], category="voice", audio=b"RIFF-fake")
    tid = p.tickets()[0]["ticket_id"]
    kinds = [item["kind"] for item in p.evidence(tid)]
    assert "audio" in kinds
    audio = [item for item in p.evidence(tid) if item["kind"] == "audio"][0]
    assert audio["audio"] == b"RIFF-fake"


def test_reporter_cooldown_is_per_account_not_device():
    p = make(cooldown=60)
    filed(p, "r1", "player-1", now=0, device_id="dev-9")
    with pytest.raises(rat.TribunalError) as exc:
        filed(p, "r2", "player-1", now=30, device_id="dev-9")
    assert exc.value.code == "cooldown"
    filed(p, "r3", "player-1", now=60, device_id="dev-9")
    assert p.ticket(p.tickets()[0]["ticket_id"])["report_count"] == 2


def test_ticket_is_routed_to_target_server():
    p = make()
    out = filed(p, "r1", server="s1", target_server="s2")
    assert out["server"] == "s2"
    assert p.ticket(out["ticket_id"])["server"] == "s2"


def test_rename_keeps_ticket_lookup_by_account():
    p = make()
    out = filed(p, "r1", target="griefer-1")
    p.rename("griefer-1", "Reformed")
    found = p.tickets(target_id="griefer-1")
    assert [t["ticket_id"] for t in found] == [out["ticket_id"]]
    assert found[0]["target_name"] == "Reformed"


def test_review_requires_reason_and_is_not_repeated():
    p = make()
    filed(p, "r1")
    tid = p.tickets()[0]["ticket_id"]
    with pytest.raises(rat.TribunalError) as exc:
        p.review(tid, "mod-1", "upheld", reason="", now=10)
    assert exc.value.code == "reason_required"
    first = p.review(tid, "mod-1", "upheld", reason="chat logs", now=11)
    assert first["status"] == "upheld"
    with pytest.raises(rat.TribunalError) as exc:
        p.review(tid, "mod-2", "dismissed", reason="second look", now=12)
    assert exc.value.code == "already_reviewed"
    assert len(p.bans("griefer-1")) == 1


def test_withdrawn_ticket_cannot_be_reviewed():
    p = make()
    filed(p, "r1", reporter="player-1")
    tid = p.tickets()[0]["ticket_id"]
    out = p.withdraw(tid, "player-1", now=5)
    assert out["status"] == "withdrawn"
    with pytest.raises(rat.TribunalError) as exc:
        p.review(tid, "mod-1", "upheld", reason="late", now=6)
    assert exc.value.code == "ticket_withdrawn"


def test_upheld_review_creates_exactly_one_ban():
    p = make(ban_duration=3600)
    for i in range(5):
        filed(p, "r%d" % i, "player-%d" % i, now=i)
    tid = p.tickets()[0]["ticket_id"]
    p.review(tid, "mod-1", "upheld", reason="confirmed", now=100)
    bans = p.bans("griefer-1")
    assert len(bans) == 1
    assert bans[0]["started_at"] == 100
    assert bans[0]["expires_at"] == 3700
    assert bans[0]["actor"] == "mod-1"


def test_expired_ban_is_lifted_by_tick():
    p = make(ban_duration=3600)
    filed(p, "r1")
    tid = p.tickets()[0]["ticket_id"]
    p.review(tid, "mod-1", "upheld", reason="confirmed", now=100)
    ban_id = p.bans("griefer-1")[0]["ban_id"]
    assert p.tick(3699)["expired"] == []
    assert p.account("griefer-1")["banned"] is True
    assert p.tick(3700)["expired"] == [ban_id]
    assert p.account("griefer-1")["banned"] is False
    assert p.tick(3701)["expired"] == []


def test_granted_appeal_revokes_ban_and_compensates_time():
    p = make(ban_duration=3600)
    filed(p, "r1")
    tid = p.tickets()[0]["ticket_id"]
    p.review(tid, "mod-1", "upheld", reason="confirmed", now=100)
    ban_id = p.bans("griefer-1")[0]["ban_id"]
    out = p.appeal(ban_id, "mod-2", granted=True, now=400, reason="mistaken id")
    assert out["status"] == "revoked"
    assert out["compensation"] == 300
    assert p.account("griefer-1")["banned"] is False
    assert p.account("griefer-1")["compensation"] == 300


def test_denied_appeal_keeps_ban_without_compensation():
    p = make(ban_duration=3600)
    filed(p, "r1")
    tid = p.tickets()[0]["ticket_id"]
    p.review(tid, "mod-1", "upheld", reason="confirmed", now=100)
    ban_id = p.bans("griefer-1")[0]["ban_id"]
    out = p.appeal(ban_id, "mod-2", granted=False, now=400, reason="no new facts")
    assert out["status"] == "upheld"
    assert p.account("griefer-1")["banned"] is True
    assert p.account("griefer-1")["compensation"] == 0


def test_replay_is_stored_per_match_and_cannot_be_overwritten():
    p = make()
    p.store_replay("match-1", {"frames": [1, 2, 3]})
    p.store_replay("match-2", {"frames": [9]})
    assert p.replay("match-1")["frames"] == [1, 2, 3]
    assert p.replay("match-2")["frames"] == [9]
    with pytest.raises(rat.TribunalError) as exc:
        p.store_replay("match-1", {"frames": [0]})
    assert exc.value.code == "replay_exists"
    assert p.replay("match-1")["frames"] == [1, 2, 3]


def test_audit_trail_records_actor_and_time_for_each_stage():
    p = make()
    filed(p, "r1", reporter="player-1", now=5)
    tid = p.tickets()[0]["ticket_id"]
    p.review(tid, "mod-1", "upheld", reason="logs", now=20)
    ban_id = p.bans("griefer-1")[0]["ban_id"]
    p.appeal(ban_id, "mod-2", granted=True, now=50, reason="mistake")
    trail = p.audit()
    assert [(e["action"], e["actor"], e["at"]) for e in trail] == [
        ("report", "player-1", 5),
        ("review", "mod-1", 20),
        ("appeal", "mod-2", 50),
    ]
