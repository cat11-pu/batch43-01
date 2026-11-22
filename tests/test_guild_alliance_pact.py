import pytest

import guild_alliance_pact as gap


def make(**kwargs):
    return gap.GuildAlliancePact(**kwargs)


def one(capacity=10):
    p = make()
    out = p.create_alliance("ALPHA", "g1", capacity=capacity)
    return p, out["alliance_id"]


def join(p, alliance_id, guild_id, now=0):
    inv = p.invite(alliance_id, guild_id, now=now, expires_at=100, version=1)
    return p.accept_invite(inv["invite_id"], guild_id, now=now, version=1)


def test_expired_invite_is_rejected():
    p, ally = one()
    inv = p.invite(ally, "g2", now=0, expires_at=10, version=1)
    with pytest.raises(gap.AllianceError) as exc:
        p.accept_invite(inv["invite_id"], "g2", now=11, version=1)
    assert exc.value.code == "EXPIRED_INVITE"
    assert p.channel_members(ally) == ["g1"]


def test_duplicate_alliance_tag_is_rejected():
    p, ally = one()
    with pytest.raises(gap.AllianceError) as exc:
        p.create_alliance("ALPHA", "g9", capacity=5)
    assert exc.value.code == "TAG_TAKEN"


def test_leaving_revokes_channel_access():
    p, ally = one()
    join(p, ally, "g2")
    p.post_message(ally, "g1", "push mid")
    p.leave("g2")
    assert p.channel_history(ally, "g2") == []
    assert p.are_friendly("g1", "g2") is False


def test_disband_ends_wars_and_clears_membership():
    p = make()
    a = p.create_alliance("ALPHA", "g1", capacity=5)["alliance_id"]
    b = p.create_alliance("BETA", "g7", capacity=5)["alliance_id"]
    join(p, a, "g2")
    p.declare_war(a, b, actor="g1", role="leader")
    p.disband(a)
    assert p.channel_members(a) == []
    assert p.are_friendly("g1", "g2") is False
    assert all(not w["active"] for w in p.wars(b))


def test_friendly_fire_only_inside_same_alliance():
    p, ally = one()
    join(p, ally, "g2")
    assert p.are_friendly("g1", "g2") is True
    assert p.are_friendly("g1", "g9") is False


def test_alliance_capacity_is_not_exceeded():
    p, ally = one(capacity=2)
    join(p, ally, "g2")
    inv = p.invite(ally, "g3", now=0, expires_at=100, version=1)
    with pytest.raises(gap.AllianceError) as exc:
        p.accept_invite(inv["invite_id"], "g3", now=0, version=1)
    assert exc.value.code == "ALLIANCE_FULL"
    assert sorted(p.channel_members(ally)) == ["g1", "g2"]


def test_guild_cannot_join_two_alliances():
    p = make()
    a = p.create_alliance("ALPHA", "g1", capacity=5)["alliance_id"]
    b = p.create_alliance("BETA", "g7", capacity=5)["alliance_id"]
    join(p, a, "g2")
    inv = p.invite(b, "g2", now=0, expires_at=100, version=1)
    with pytest.raises(gap.AllianceError) as exc:
        p.accept_invite(inv["invite_id"], "g2", now=0, version=1)
    assert exc.value.code == "GUILD_ALREADY_IN_ALLIANCE"


def test_kill_is_scored_once_per_event_id():
    p = make()
    a = p.create_alliance("ALPHA", "g1", capacity=5)["alliance_id"]
    b = p.create_alliance("BETA", "g7", capacity=5)["alliance_id"]
    p.declare_war(a, b, actor="g1", role="leader")
    first = p.record_kill("evt-1", "g1", "g7")
    second = p.record_kill("evt-1", "g1", "g7")
    assert first["scored"] is True
    assert second["scored"] is False
    assert second["duplicate"] is True


def test_ordinary_member_cannot_declare_war():
    p = make()
    a = p.create_alliance("ALPHA", "g1", capacity=5)["alliance_id"]
    b = p.create_alliance("BETA", "g7", capacity=5)["alliance_id"]
    with pytest.raises(gap.AllianceError) as exc:
        p.declare_war(a, b, actor="g2", role="member")
    assert exc.value.code == "PERMISSION_DENIED"
    assert p.wars(a) == []


def test_history_is_hidden_from_exited_guild():
    p, ally = one()
    join(p, ally, "g2")
    p.post_message(ally, "g1", "secret plan")
    assert p.channel_history(ally, "g2") == ["secret plan"]
    p.leave("g2")
    assert p.channel_history(ally, "g2") == []


def test_merge_rewrites_war_targets():
    p = make()
    a = p.create_alliance("ALPHA", "g1", capacity=5)["alliance_id"]
    b = p.create_alliance("BETA", "g7", capacity=5)["alliance_id"]
    p.declare_war(a, b, actor="g1", role="leader")
    mapping = p.merge_servers({b: "s2-b"})
    assert mapping[b] == "s2-b"
    assert [w["target"] for w in p.wars(a)] == ["s2-b"]
    assert p.channel_members("s2-b") == ["g7"]


def test_warehouse_is_archived_on_disband():
    p, ally = one()
    join(p, ally, "g2")
    p.warehouse_contrib(ally, "g1", 60)
    p.warehouse_contrib(ally, "g2", 40)
    out = p.disband(ally)
    assert out["archived_warehouse"] == 100


def test_second_alliance_join_is_refused_after_accept():
    p = make()
    a = p.create_alliance("ALPHA", "g1", capacity=5)["alliance_id"]
    b = p.create_alliance("BETA", "g7", capacity=5)["alliance_id"]
    join(p, a, "g2")
    join(p, b, "g3")
    inv = p.invite(b, "g2", now=0, expires_at=100, version=1)
    with pytest.raises(gap.AllianceError):
        p.accept_invite(inv["invite_id"], "g2", now=0, version=1)
    assert "g2" in p.channel_members(a)
    assert "g2" not in p.channel_members(b)


def test_disband_leaves_no_active_war_or_friendly_flag():
    p = make()
    a = p.create_alliance("ALPHA", "g1", capacity=5)["alliance_id"]
    b = p.create_alliance("BETA", "g7", capacity=5)["alliance_id"]
    join(p, a, "g2")
    p.declare_war(a, b, actor="g1", role="leader")
    p.disband(a)
    assert all(not w["active"] for w in p.wars(b))
    assert p.channel_members(a) == []
    assert p.are_friendly("g1", "g2") is False


def test_invite_version_mismatch_is_rejected():
    p, ally = one()
    inv = p.invite(ally, "g2", now=0, expires_at=100, version=2)
    with pytest.raises(gap.AllianceError) as exc:
        p.accept_invite(inv["invite_id"], "g2", now=0, version=1)
    assert exc.value.code == "INVITE_STALE"
    assert p.channel_members(ally) == ["g1"]
