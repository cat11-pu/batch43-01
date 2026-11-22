import pytest

import raid_signup_waitlist as rsw

CAP = {"tank": 1, "healer": 1, "dps": 3}


def make(**kwargs):
    return rsw.RaidSignupWaitlist(**kwargs)


def cycle(r, raid="raid1", capacity=None, lock_at=1000, now=0):
    return r.open_cycle(raid, capacity or dict(CAP), lock_at, now=now)


def ids(rows):
    return [row["character_id"] if isinstance(row, dict) else row
            for row in rows]


def test_promotion_reserves_slot_before_notifying():
    r = make()
    cycle(r)
    r.signup("raid1", "h1", "healer", account_id="a1", now=1)
    r.signup("raid1", "h2", "healer", account_id="a2", now=2)
    out = r.promote_next("raid1", role="healer")
    assert out["promoted"] == "h2"
    assert out["notified"] is True
    assert "h2" in ids(r.roster("raid1"))
    assert r.waitlist("raid1") == []
    second = r.promote_next("raid1", role="healer")
    assert second["promoted"] is None


def test_two_officers_racing_for_last_slot_only_one_wins():
    r = make()
    cycle(r)
    r.signup("raid1", "h1", "healer", account_id="a1", now=1)
    r.signup("raid1", "h2", "healer", account_id="a2", now=2)
    r.promote_next("raid1", role="healer")
    with pytest.raises(rsw.RaidError) as exc:
        r.promote_next("raid1", role="healer")
    assert exc.value.code == "NO_SLOT"
    assert len(r.roster("raid1")) == 1


def test_signup_after_lock_is_rejected():
    r = make()
    cycle(r, lock_at=100)
    with pytest.raises(rsw.RaidError) as exc:
        r.signup("raid1", "d1", "dps", account_id="a1", now=100)
    assert exc.value.code == "SIGNUP_LOCKED"
    assert r.roster("raid1") == []


def test_role_change_frees_old_quota():
    r = make()
    cycle(r)
    r.signup("raid1", "h1", "healer", account_id="a1", now=1)
    out = r.change_role("raid1", "h1", "dps")
    assert out["role"] == "dps"
    r.signup("raid1", "h2", "healer", account_id="a2", now=2)
    assert "h2" in ids(r.roster("raid1"))


def test_same_account_second_character_is_rejected():
    r = make()
    cycle(r)
    r.signup("raid1", "d1", "dps", account_id="acc", now=1)
    with pytest.raises(rsw.RaidError) as exc:
        r.signup("raid1", "d2", "dps", account_id="acc", now=2)
    assert exc.value.code == "DUPLICATE_ACCOUNT"
    assert "d2" not in ids(r.roster("raid1"))


def test_waitlisted_player_gets_no_deserter_penalty():
    r = make()
    cycle(r, capacity={"tank": 1, "healer": 1, "dps": 1})
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    r.signup("raid1", "d2", "dps", account_id="a2", now=2)
    assert r.abandon("raid1", "d2")["deserter"] is False
    assert r.abandon("raid1", "d1")["deserter"] is True


def test_lock_uses_server_time_only():
    r = make()
    cycle(r, lock_at=1000)
    assert r.is_locked("raid1", now=999) is False
    assert r.is_locked("raid1", now=1000) is True


def test_leaving_frees_slot_for_new_signup():
    r = make()
    cycle(r, capacity={"tank": 1, "healer": 1, "dps": 1})
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    r.leave("raid1", "d1")
    assert r.roster("raid1") == []
    r.signup("raid1", "d2", "dps", account_id="a2", now=2)
    assert "d2" in ids(r.roster("raid1"))


def test_roster_copy_filters_transferred_characters():
    r = make()
    cycle(r, raid="raid1")
    cycle(r, raid="raid2")
    r.signup("raid1", "c1", "dps", account_id="a1", now=1)
    r.signup("raid1", "c2", "dps", account_id="a2", now=2)
    r.copy_roster("raid1", "raid2", valid_characters={"c1"})
    assert ids(r.roster("raid2")) == ["c1"]


def test_cancel_clears_signups_but_keeps_audit():
    r = make()
    cycle(r)
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    r.cancel_raid("raid1")
    snap = r.snapshot("raid1")
    assert snap["roster"] == []
    assert snap["waitlist"] == []
    assert len(snap["audit"]) >= 1


def test_leave_after_lock_is_refused():
    r = make()
    cycle(r, lock_at=100)
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    r.lock("raid1", now=100)
    with pytest.raises(rsw.RaidError) as exc:
        r.leave("raid1", "d1")
    assert exc.value.code == "ROSTER_LOCKED"
    assert "d1" in ids(r.roster("raid1"))


def test_waitlisted_player_gets_no_boss_progress():
    r = make()
    cycle(r, capacity={"tank": 1, "healer": 1, "dps": 1})
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    r.signup("raid1", "d2", "dps", account_id="a2", now=2)
    out = r.record_kill("raid1", "boss-1")
    assert out["progress"]["d1"] == 1
    assert "d2" not in out["progress"]


def test_roster_never_exceeds_capacity():
    r = make()
    cycle(r, capacity={"tank": 1, "healer": 1, "dps": 1})
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    r.signup("raid1", "d2", "dps", account_id="a2", now=2)
    try:
        r.promote_next("raid1", role="dps")
    except rsw.RaidError:
        pass
    assert len(r.roster("raid1")) == 1


def test_character_cannot_be_on_roster_and_waitlist_together():
    r = make()
    cycle(r, capacity={"tank": 1, "healer": 1, "dps": 1})
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    assert "d1" in ids(r.roster("raid1"))
    assert "d1" not in ids(r.waitlist("raid1"))


def test_restart_restores_roster_and_lock():
    r = make()
    cycle(r, lock_at=100)
    r.signup("raid1", "d1", "dps", account_id="a1", now=1)
    r.lock("raid1", now=100)
    state = r.dump()
    restored = make()
    restored.load(state)
    assert "d1" in ids(restored.roster("raid1"))
    assert restored.is_locked("raid1", now=200) is True
