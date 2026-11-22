import pytest

import invite_pending_book as ipb


def make(**kwargs):
    return ipb.InvitePendingBook(**kwargs)


def team(p, team_id="t1", leader="leader-1", capacity=5):
    p.create_team(team_id, leader, capacity=capacity)
    return team_id


def sent(p, invite="i1", team_id="t1", from_account="leader-1",
         to_account="acc-1", now=0):
    return p.send(invite, team_id, from_account, to_account, now=now)


def test_duplicate_send_returns_the_existing_invite():
    p = make()
    team(p)
    first = sent(p, "i1", to_account="acc-1")
    again = sent(p, "i2", to_account="acc-1", now=1)
    assert again["status"] == "already_invited"
    assert again["invite_id"] == first["invite_id"]
    assert len(p.pending("acc-1")) == 1


def test_accept_joins_team_and_cannot_be_replayed():
    p = make()
    team(p)
    sent(p)
    out = p.accept("i1", "acc-1", now=1)
    assert out["status"] == "accepted"
    assert "acc-1" in out["members"]
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i1", "acc-1", now=2)
    assert exc.value.code == "already_joined"
    assert p.members("t1").count("acc-1") == 1


def test_expired_invite_cannot_be_accepted():
    p = make(ttl=60)
    team(p)
    sent(p, now=0)
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i1", "acc-1", now=61)
    assert exc.value.code == "invite_expired"
    assert "acc-1" not in p.members("t1")


def test_disbanded_team_invalidates_pending_invite():
    p = make()
    team(p)
    sent(p)
    p.disband("t1", "leader-1", now=5)
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i1", "acc-1", now=6)
    assert exc.value.code == "team_disbanded"


def test_blocked_account_cannot_be_invited():
    p = make()
    team(p)
    p.block("acc-1", "leader-1")
    with pytest.raises(ipb.InviteError) as exc:
        sent(p)
    assert exc.value.code == "blocked"
    assert p.pending("acc-1") == []


def test_block_after_send_prevents_acceptance():
    p = make()
    team(p)
    sent(p)
    p.block("acc-1", "leader-1")
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i1", "acc-1", now=1)
    assert exc.value.code == "blocked"
    assert "acc-1" not in p.members("t1")


def test_full_team_rejects_acceptance_atomically():
    p = make()
    team(p, capacity=2)
    sent(p, "i1", to_account="acc-1")
    sent(p, "i2", to_account="acc-2")
    p.accept("i2", "acc-2", now=1)
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i1", "acc-1", now=2)
    assert exc.value.code == "team_full"
    assert p.members("t1") == ["leader-1", "acc-2"]


def test_account_already_in_a_team_cannot_join_another():
    p = make()
    team(p, "t1", "leader-1")
    team(p, "t2", "leader-2")
    sent(p, "i1", "t1", "leader-1", "acc-1")
    sent(p, "i2", "t2", "leader-2", "acc-1")
    p.accept("i1", "acc-1", now=1)
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i2", "acc-1", now=2)
    assert exc.value.code == "already_joined"
    assert "acc-1" not in p.members("t2")


def test_two_invites_for_same_account_only_one_joins():
    p = make()
    team(p, "t1", "leader-1")
    team(p, "t2", "leader-2")
    sent(p, "i1", "t1", "leader-1", "acc-9")
    sent(p, "i2", "t2", "leader-2", "acc-9")
    p.accept("i1", "acc-9", now=1)
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i2", "acc-9", now=2)
    assert exc.value.code == "already_joined"


def test_accept_inside_dungeon_is_refused():
    p = make()
    team(p)
    sent(p)
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i1", "acc-1", now=1, in_dungeon=True)
    assert exc.value.code == "in_dungeon"
    assert "acc-1" not in p.members("t1")


def test_mutual_invites_do_not_create_a_party_cycle():
    p = make()
    team(p, "t1", "A")
    team(p, "t2", "B")
    sent(p, "i1", "t1", "A", "B")
    sent(p, "i2", "t2", "B", "A")
    p.accept("i1", "B", now=1)
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i2", "A", now=2)
    assert exc.value.code == "already_joined"
    assert p.members("t1") == ["A", "B"]


def test_decline_cooldown_prevents_immediate_resend():
    p = make(decline_cooldown=30)
    team(p)
    sent(p, now=0)
    p.decline("i1", "acc-1", now=10)
    with pytest.raises(ipb.InviteError) as exc:
        sent(p, "i2", to_account="acc-1", now=20)
    assert exc.value.code == "cooldown"
    sent(p, "i3", to_account="acc-1", now=40)
    assert len(p.pending("acc-1")) == 1


def test_cross_server_invite_routes_by_account_id():
    p = make()
    team(p)
    sent(p, "i1", to_account="s2:acc-9")
    assert [row["invite_id"] for row in p.pending("s2:acc-9")] == ["i1"]
    out = p.accept("i1", "s2:acc-9", now=1)
    assert out["status"] == "accepted"
    assert "s2:acc-9" in p.members("t1")


def test_explicit_expire_blocks_later_accept():
    p = make()
    team(p)
    sent(p)
    out = p.expire("i1", now=5)
    assert out["status"] == "expired"
    with pytest.raises(ipb.InviteError) as exc:
        p.accept("i1", "acc-1", now=6)
    assert exc.value.code == "invite_expired"
    assert p.pending("acc-1") == []


def test_restore_keeps_pending_and_drops_accepted_invites():
    p = make()
    team(p)
    sent(p, "i1", to_account="acc-1")
    sent(p, "i2", to_account="acc-2")
    p.accept("i2", "acc-2", now=1)
    state = p.dump()
    q = make()
    q.load(state)
    assert [row["invite_id"] for row in q.pending("acc-1")] == ["i1"]
    assert q.pending("acc-2") == []
    assert q.accept("i1", "acc-1", now=2)["status"] == "accepted"
    with pytest.raises(ipb.InviteError) as exc:
        q.accept("i2", "acc-2", now=3)
    assert exc.value.code == "unknown_invite"
