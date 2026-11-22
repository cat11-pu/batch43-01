import pytest

import territory_siege_book as tsb


def make(**kwargs):
    return tsb.TerritorySiegeBook(**kwargs)


def world(p):
    p.create_territory("t1", "guild-a", buildings=["keep", "barracks"],
                       permission=["guild-a"])
    for guild in ("guild-a", "guild-b", "guild-c"):
        p.fund(guild, 1000)
    return p


def test_second_declaration_in_same_window_is_rejected():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    with pytest.raises(tsb.SiegeError) as exc:
        p.declare("t1", "guild-c", now=10)
    assert exc.value.code == "already_contested"
    assert p.territory("t1")["contest"]["guild"] == "guild-b"


def test_failed_declarer_is_never_charged():
    p = make(window=60, declare_cost=100)
    world(p)
    p.declare("t1", "guild-b", now=0)
    before = p.guild_account("guild-c")
    with pytest.raises(tsb.SiegeError):
        p.declare("t1", "guild-c", now=10)
    assert p.guild_account("guild-c") == before


def test_repeated_declaration_by_same_guild_charges_once():
    p = make(window=60, declare_cost=100)
    world(p)
    start = p.guild_account("guild-b")
    p.declare("t1", "guild-b", now=0)
    with pytest.raises(tsb.SiegeError) as exc:
        p.declare("t1", "guild-b", now=5)
    assert exc.value.code == "already_contested"
    assert p.guild_account("guild-b") == start - 100


def test_settle_inside_open_window_is_refused():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    with pytest.raises(tsb.SiegeError) as exc:
        p.settle("t1", "guild-b", now=30)
    assert exc.value.code == "window_open"
    assert p.territory("t1")["owner"] == "guild-a"


def test_settlement_transfers_ownership_and_scores_once():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    out = p.settle("t1", "guild-b", now=60, idempotency_key="settle-1")
    assert out["status"] == "settled"
    assert out["previous_owner"] == "guild-a"
    assert p.territory("t1")["owner"] == "guild-b"
    assert p.territory("t1")["buildings"] == []
    assert p.guild_score("guild-b") == 1


def test_replayed_settlement_is_idempotent_by_key():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    p.settle("t1", "guild-b", now=60, idempotency_key="settle-1")
    replay = p.settle("t1", "guild-b", now=61, idempotency_key="settle-1")
    assert replay["status"] == "replayed"
    assert p.territory("t1")["owner"] == "guild-b"
    assert p.guild_score("guild-b") == 1


def test_disqualified_defender_cannot_be_declared_winner():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    with pytest.raises(tsb.SiegeError) as exc:
        p.settle("t1", "guild-a", now=60, idempotency_key="k1",
                 defender_qualified=False)
    assert exc.value.code == "defender_disqualified"
    assert p.territory("t1")["owner"] == "guild-a"
    assert p.guild_score("guild-a") == 0


def test_transfer_clears_old_buildings_and_permissions():
    p = make()
    world(p)
    out = p.transfer("t1", "guild-b", "gm-1", now=5)
    assert out["status"] == "transferred"
    t = p.territory("t1")
    assert t["owner"] == "guild-b"
    assert t["buildings"] == []
    assert t["permission"] == ["guild-b"]


def test_first_tax_period_pays_the_current_owner():
    p = make(tax_interval=100, tax_amount=10)
    world(p)
    p.tick(99)
    assert p.guild_account("guild-a") == 1000
    p.tick(100)
    assert p.guild_account("guild-a") == 1010
    p.tick(150)
    assert p.guild_account("guild-a") == 1010
    p.tick(200)
    assert p.guild_account("guild-a") == 1020


def test_duplicate_battle_result_does_not_double_guild_score():
    p = make()
    world(p)
    first = p.battle_result("bf-1", "guild-a", 10, player="p1")
    assert first["status"] == "recorded"
    again = p.battle_result("bf-1", "guild-a", 10, player="p1")
    assert again["status"] == "duplicate_result"
    assert p.guild_score("guild-a") == 10


def test_new_challenger_allowed_after_window_lapses():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    out = p.declare("t1", "guild-c", now=61)
    assert out["status"] == "declared"
    assert p.territory("t1")["contest"]["guild"] == "guild-c"


def test_season_reset_clears_territory_and_leaderboard_together():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    p.settle("t1", "guild-b", now=60, idempotency_key="s1")
    out = p.season_reset()
    assert out["leaderboard_cleared"] is True
    assert p.territory("t1")["owner"] is None
    assert p.territory("t1")["buildings"] == []
    assert p.guild_score("guild-b") == 0


def test_restore_keeps_ownership_after_restart():
    p = make(window=60)
    world(p)
    p.declare("t1", "guild-b", now=0)
    p.settle("t1", "guild-b", now=60, idempotency_key="s1")
    state = p.snapshot()
    q = make(window=60)
    q.restore(state)
    assert q.territory("t1")["owner"] == "guild-b"
    assert q.guild_score("guild-b") == 1


def test_insufficient_funds_blocks_declaration():
    p = make(declare_cost=100)
    p.create_territory("t1", "guild-a")
    p.fund("guild-b", 50)
    with pytest.raises(tsb.SiegeError) as exc:
        p.declare("t1", "guild-b", now=0)
    assert exc.value.code == "insufficient_funds"
    assert p.territory("t1")["contest"] is None


def test_settle_without_active_contest_is_rejected():
    p = make()
    world(p)
    with pytest.raises(tsb.SiegeError) as exc:
        p.settle("t1", "guild-b", now=60, idempotency_key="k1")
    assert exc.value.code == "not_contested"
    assert p.territory("t1")["owner"] == "guild-a"
