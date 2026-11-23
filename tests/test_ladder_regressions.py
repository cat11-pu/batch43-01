import pytest

import ladder_board_snapshot as lbs


def make(**kwargs):
    return lbs.LadderBoardSnapshot(**kwargs)


def ids(rows):
    return [(row["server"], row["player"]) for row in rows]


def test_ban_triggers_live_recompute_and_new_snapshot():
    board = make()
    board.seed(("s1", "p1"), 1200)
    board.seed(("s1", "p2"), 1100)
    board.seed(("s1", "p3"), 1000)
    board.ban(("s1", "p1"))
    assert ids(board.ranking()) == [("s1", "p2"), ("s1", "p3")]
    snap = board.snapshot()
    rewards = board.rewards(snap["snapshot_id"])
    assert [row["player"] for row in rewards][:2] == ["p2", "p3"]
    with pytest.raises(lbs.LadderError) as exc:
        board.settle("m1", ("s1", "p1"), ("s1", "p2"), now=1)
    assert exc.value.code == "player_banned"


def test_frozen_snapshot_is_unaffected_by_later_ban():
    board = make()
    board.seed(("s1", "p1"), 1200)
    board.seed(("s1", "p2"), 1100)
    snap = board.snapshot(now=0)
    board.ban(("s1", "p1"), now=5)
    rewards = board.rewards(snap["snapshot_id"])
    assert [row["player"] for row in rewards] == ["p1", "p2"]


def test_decay_stays_idempotent_across_restart():
    board = make()
    board.seed(("s1", "p1"), 1200)
    board.decay("2026-11-01", amount=5, now=0)
    restored = make()
    restored.load(board.dump())
    restored.decay("2026-11-01", amount=5, now=1)
    assert restored.ranking()[0]["points"] == 1195
    restored.decay("2026-11-02", amount=5, now=2)
    assert restored.ranking()[0]["points"] == 1190


def test_cross_server_id_collision_keeps_independent_rows():
    board = make()
    board.seed(("s1", "p1"), 1000)
    board.seed(("s2", "p1"), 1000)
    board.settle("m1", ("s2", "p1"), ("s1", "p1"), now=0)
    points = {(row["server"], row["player"]): row["points"]
              for row in board.ranking()}
    assert points == {("s2", "p1"): 1016, ("s1", "p1"): 984}


def test_pagination_never_repeats_rows_after_drop():
    board = make()
    for name in ("p1", "p2", "p3", "p4", "p5"):
        board.seed(("s1", name), 1000 + int(name[1:]))
    board.remove(("s1", "p5"))
    seen = []
    for offset in (0, 2, 4):
        seen.extend(ids(board.ranking(limit=2, offset=offset)))
    assert len(seen) == len(set(seen)) == 4


def test_rewards_require_a_persisted_snapshot():
    board = make()
    board.seed(("s1", "p1"), 1000)
    with pytest.raises(lbs.LadderError) as exc:
        board.rewards("missing")
    assert exc.value.code == "snapshot_not_found"


def test_old_season_ranking_is_immutable_after_switch():
    board = make()
    board.seed(("s1", "p1"), 1000)
    board.seed(("s1", "p2"), 1000)
    board.settle("m1", ("s1", "p1"), ("s1", "p2"), now=10)
    board.season_switch("s2", freeze_at=1000, now=1000)
    board.seed(("s1", "p1"), 5000)
    old = board.ranking(season="s1")
    assert [(row["player"], row["points"]) for row in old] == [
        ("p1", 1016), ("p2", 984)]
    assert board.ranking(season="s2")[0]["points"] == 5000


def test_unknown_match_players_are_rejected():
    board = make()
    board.seed(("s1", "p1"), 1000)
    with pytest.raises(lbs.LadderError) as exc:
        board.settle("m1", ("s1", "p1"), ("s1", "ghost"), now=0)
    assert exc.value.code == "unknown_player"


def test_settlement_in_new_season_starts_new_players_at_base_rating():
    board = make()
    board.seed(("s1", "p1"), 1000)
    board.seed(("s1", "p2"), 1000)
    board.season_switch("s2", freeze_at=1000, now=1000)
    out = board.settle(
        "m1", ("s1", "p2"), ("s1", "p1"), now=1001)
    assert out["season"] == "s2"
    assert out["winner_points"] == 1016
    assert out["loser_points"] == 984
