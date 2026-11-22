import pytest

import lfg_recruit_board as lfg


def make(**kwargs):
    return lfg.LfgRecruitBoard(**kwargs)


def open_listing(p, listing="l1", leader="leader-1", capacity=5, now=0,
                 dungeon="dungeon-x", pinned=False, requirements=None):
    return p.create(listing, leader, capacity=capacity, now=now,
                    dungeon=dungeon, pinned=pinned, region="r1",
                    requirements=requirements)


def applied(p, app="a1", listing="l1", account="acc-1", now=0, **kwargs):
    return p.apply(app, listing, account, now=now, **kwargs)


def test_repeated_create_by_same_leader_returns_one_listing():
    p = make()
    first = open_listing(p, "l1")
    second = open_listing(p, "l2")
    assert second["status"] == "existing"
    assert second["listing_id"] == first["listing_id"]
    assert len(p.listings()) == 1


def test_accept_occupies_slot_immediately_and_full_listing_delists():
    p = make()
    open_listing(p, capacity=2)
    applied(p)
    out = p.accept("a1", "leader-1", now=1)
    assert out["status"] == "accepted"
    assert "acc-1" in out["members"]
    assert len(p.members("l1")) == 2
    assert p.get_listing("l1")["status"] == "closed"
    assert p.listings() == []


def test_apply_to_full_listing_is_rejected():
    p = make()
    open_listing(p, capacity=2)
    applied(p, "a1", account="acc-1")
    p.accept("a1", "leader-1", now=1)
    with pytest.raises(lfg.LfgError) as exc:
        applied(p, "a2", account="acc-2", now=2)
    assert exc.value.code == "roster_full"


def test_two_accepts_for_last_slot_only_one_wins():
    p = make()
    open_listing(p, capacity=2)
    applied(p, "a1", account="acc-1")
    applied(p, "a2", account="acc-2")
    first = p.accept("a1", "leader-1", now=1)
    assert first["status"] == "accepted"
    with pytest.raises(lfg.LfgError) as exc:
        p.accept("a2", "leader-1", now=1)
    assert exc.value.code == "roster_full"
    assert [m["account"] for m in p.members("l1")] == ["leader-1", "acc-1"]
    pending = {a["application_id"]: a for a in p.applications("l1")}
    assert pending["a2"]["status"] == "pending"


def test_accepting_the_same_application_twice_is_idempotent():
    p = make(capacity=5)
    open_listing(p)
    applied(p)
    p.accept("a1", "leader-1", now=1)
    out = p.accept("a1", "leader-1", now=2)
    assert out["status"] == "already_accepted"
    assert len(p.members("l1")) == 2


def test_expired_application_cannot_be_accepted():
    p = make(application_ttl=300)
    open_listing(p)
    applied(p, now=0)
    with pytest.raises(lfg.LfgError) as exc:
        p.accept("a1", "leader-1", now=301)
    assert exc.value.code == "application_expired"
    assert len(p.members("l1")) == 1


def test_requirements_change_makes_old_application_stale():
    p = make()
    open_listing(p, requirements={"role": "healer"})
    applied(p, role="healer", now=0)
    p.update("l1", "leader-1", requirements={"role": "dps"}, now=1)
    with pytest.raises(lfg.LfgError) as exc:
        p.accept("a1", "leader-1", now=2)
    assert exc.value.code == "stale_application"
    fresh = applied(p, "a2", account="acc-2", role="dps", now=3)
    assert fresh["version"] == p.get_listing("l1")["version"]
    assert p.accept("a2", "leader-1", now=4)["status"] == "accepted"


def test_dungeon_lockout_blocks_application():
    p = make()
    open_listing(p, dungeon="dungeon-x")
    with pytest.raises(lfg.LfgError) as exc:
        applied(p, lockouts=["dungeon-x"], now=0)
    assert exc.value.code == "lockout"


def test_second_character_of_same_account_cannot_apply_twice():
    p = make()
    open_listing(p)
    applied(p, "a1", account="acc-1", character="char-a")
    with pytest.raises(lfg.LfgError) as exc:
        applied(p, "a2", account="acc-1", character="char-b", now=1)
    assert exc.value.code == "already_applied"


def test_account_already_in_another_team_cannot_apply():
    p = make()
    open_listing(p, "l1", "leader-1")
    open_listing(p, "l2", "leader-2")
    applied(p, "a1", "l1", account="acc-1")
    p.accept("a1", "leader-1", now=1)
    with pytest.raises(lfg.LfgError) as exc:
        applied(p, "a2", "l2", account="acc-1", now=2)
    assert exc.value.code == "already_in_team"


def test_title_edit_keeps_accepted_members():
    p = make()
    open_listing(p)
    applied(p)
    p.accept("a1", "leader-1", now=1)
    p.update("l1", "leader-1", title="new note", now=2)
    assert [m["account"] for m in p.members("l1")] == ["leader-1", "acc-1"]


def test_pinned_listing_stays_first_after_refresh():
    p = make()
    open_listing(p, "l1", "leader-1", pinned=False)
    open_listing(p, "l2", "leader-2", pinned=True)
    first = p.listings()
    second = p.listings()
    assert [row["listing_id"] for row in first] == ["l2", "l1"]
    assert [row["listing_id"] for row in second] == ["l2", "l1"]


def test_explicit_expire_blocks_later_accept():
    p = make()
    open_listing(p)
    applied(p)
    out = p.expire("a1", now=10)
    assert out["status"] == "expired"
    with pytest.raises(lfg.LfgError) as exc:
        p.accept("a1", "leader-1", now=11)
    assert exc.value.code == "application_expired"


def test_cancelled_listing_invalidates_pending_applications():
    p = make()
    open_listing(p)
    applied(p)
    p.cancel("l1", "leader-1", now=5)
    with pytest.raises(lfg.LfgError) as exc:
        p.accept("a1", "leader-1", now=6)
    assert exc.value.code == "listing_closed"
    assert p.applications("l1")[0]["status"] == "cancelled"


def test_restore_keeps_pending_applications_and_occupied_slots():
    p = make()
    open_listing(p, capacity=3)
    applied(p, "a1", account="acc-1")
    applied(p, "a2", account="acc-2")
    p.accept("a1", "leader-1", now=1)
    state = p.snapshot()
    q = make()
    q.restore(state)
    assert len(q.members("l1")) == 2
    assert q.accept("a2", "leader-1", now=2)["status"] == "accepted"
    assert len(q.members("l1")) == 3
    with pytest.raises(lfg.LfgError) as exc:
        q.apply("a3", "l1", "acc-3", now=3)
    assert exc.value.code == "roster_full"
