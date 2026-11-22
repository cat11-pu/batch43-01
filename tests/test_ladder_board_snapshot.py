import pytest

import ladder_board_snapshot as lbs


def make(**kwargs):
    return lbs.LadderBoardSnapshot(**kwargs)


def ids(rows):
    return [(row["server"], row["player"]) for row in rows]


def test_equal_rating_match_is_zero_sum():
    p = make()
    p.seed(("s1", "p1"), 1000)
    p.seed(("s1", "p2"), 1000)
    out = p.settle("m1", ("s1", "p1"), ("s1", "p2"), now=0)
    assert out["winner_points"] == 1016
    assert out["loser_points"] == 984
    rows = {row["player"]: row["points"] for row in p.ranking()}
    assert rows == {"p1": 1016, "p2": 984}


def test_duplicate_match_settlement_is_rejected():
    p = make()
    p.seed(("s1", "p1"), 1000)
    p.seed(("s1", "p2"), 1000)
    p.settle("m1", ("s1", "p1"), ("s1", "p2"), now=0)
    with pytest.raises(lbs.LadderError) as exc:
        p.settle("m1", ("s1", "p1"), ("s1", "p2"), now=1)
    assert exc.value.code == "already_settled"
    rows = {row["player"]: row["points"] for row in p.ranking()}
    assert rows == {"p1": 1016, "p2": 984}


def test_player_keeps_exactly_one_rating_row():
    p = make()
    p.seed(("s1", "p1"), 1000)
    for index, opponent in enumerate(("p2", "p3", "p4")):
        p.seed(("s1", opponent), 1000)
        p.settle("m%d" % index, ("s1", "p1"), ("s1", opponent), now=index)
    rows = [row for row in p.ranking() if row["player"] == "p1"]
    assert len(rows) == 1
    assert len(p.ranking()) == 4


def test_cross_server_same_player_id_is_two_rows():
    p = make()
    p.seed(("s1", "p1"), 1000)
    p.seed(("s2", "p1"), 1000)
    p.seed(("s2", "p2"), 1000)
    p.settle("m1", ("s2", "p1"), ("s2", "p2"), now=0)
    rows = p.ranking()
    assert ids(rows) == [("s2", "p1"), ("s1", "p1"), ("s2", "p2")]
    assert rows[0]["points"] == 1016
    assert rows[1]["points"] == 1000


def test_tie_ordering_is_stable_and_uses_documented_key():
    p = make()
    p.seed(("s1", "p1"), 1100, wins=1)
    p.seed(("s1", "p2"), 1100, wins=2)
    first = ids(p.ranking())
    second = ids(p.ranking())
    assert first == second == [("s1", "p2"), ("s1", "p1")]


def test_same_timestamp_ties_are_deterministic():
    p = make()
    for name in ("p1", "p2", "p3", "p4"):
        p.seed(("s1", name), 1000)
    p.settle("m1", ("s1", "p1"), ("s1", "p3"), now=0)
    p.settle("m2", ("s1", "p2"), ("s1", "p4"), now=0)
    assert ids(p.ranking())[:2] == [("s1", "p1"), ("s1", "p2")]
    assert ids(p.ranking()) == ids(p.ranking())


def test_ban_removes_player_from_ranking():
    p = make()
    p.seed(("s1", "p1"), 1200)
    p.seed(("s1", "p2"), 1100)
    p.ban(("s1", "p1"), now=10)
    assert ids(p.ranking()) == [("s1", "p2")]


def test_remove_triggers_ranking_recompute():
    p = make()
    p.seed(("s1", "p1"), 1200)
    p.seed(("s1", "p2"), 1100)
    p.remove(("s1", "p1"))
    assert ids(p.ranking()) == [("s1", "p2")]
    with pytest.raises(lbs.LadderError) as exc:
        p.settle("m9", ("s1", "p1"), ("s1", "p2"), now=1)
    assert exc.value.code == "unknown_player"


def test_snapshot_rewards_are_frozen_against_later_settles():
    p = make()
    p.seed(("s1", "p1"), 1200)
    p.seed(("s1", "p2"), 1100)
    snap = p.snapshot(now=0)
    p.settle("m1", ("s1", "p2"), ("s1", "p1"), now=1)
    rewards = p.rewards(snap["snapshot_id"])
    assert [(row["player"], row["reward"]) for row in rewards] == [
        ("p1", 100), ("p2", 50)]


def test_rewards_default_to_latest_persisted_snapshot():
    p = make()
    p.seed(("s1", "p1"), 1300)
    p.seed(("s1", "p2"), 1200)
    first = p.snapshot(now=0)
    p.seed(("s1", "p3"), 9999)
    second = p.snapshot(now=1)
    assert second["snapshot_id"] != first["snapshot_id"]
    assert p.rewards() == p.rewards(second["snapshot_id"])
    assert p.rewards(first["snapshot_id"])[0]["player"] == "p1"


def test_post_freeze_settlement_belongs_to_new_season():
    p = make()
    p.seed(("s1", "p1"), 1000)
    p.seed(("s1", "p2"), 1000)
    p.settle("m1", ("s1", "p1"), ("s1", "p2"), now=500)
    p.season_switch("s2", freeze_at=1000, now=1000)
    p.settle("m2", ("s1", "p2"), ("s1", "p1"), now=1001)
    assert p.ranking()[0]["player"] == "p2"
    assert p.ranking()[0]["points"] == 1016
    old = p.ranking(season="s1")
    assert [(r["player"], r["points"]) for r in old] == [
        ("p1", 1016), ("p2", 984)]


def test_pre_freeze_settlement_after_switch_is_rejected():
    p = make()
    p.seed(("s1", "p1"), 1000)
    p.seed(("s1", "p2"), 1000)
    p.season_switch("s2", freeze_at=1000, now=1000)
    with pytest.raises(lbs.LadderError) as exc:
        p.settle("m1", ("s1", "p1"), ("s1", "p2"), now=999)
    assert exc.value.code == "season_closed"


def test_decay_applies_at_most_once_per_day():
    p = make()
    p.seed(("s1", "p1"), 1200)
    p.decay("2026-11-01", amount=5, now=0)
    p.decay("2026-11-01", amount=5, now=1)
    assert p.ranking()[0]["points"] == 1195
    p.decay("2026-11-02", amount=5, now=2)
    assert p.ranking()[0]["points"] == 1190


def test_pagination_does_not_repeat_players_after_a_drop():
    p = make()
    for name in ("p1", "p2", "p3", "p4"):
        p.seed(("s1", name), 1000 + int(name[1:]))
    page1 = ids(p.ranking(limit=2, offset=0))
    page2 = ids(p.ranking(limit=2, offset=2))
    assert page1 == [("s1", "p4"), ("s1", "p3")]
    assert page2 == [("s1", "p2"), ("s1", "p1")]
    p.remove(("s1", "p4"))
    page1 = ids(p.ranking(limit=2, offset=0))
    page2 = ids(p.ranking(limit=2, offset=2))
    assert len(set(page1 + page2)) == 3


def test_restore_keeps_ratings_and_settlement_idempotency():
    p = make()
    p.seed(("s1", "p1"), 1000)
    p.seed(("s1", "p2"), 1000)
    p.settle("m1", ("s1", "p1"), ("s1", "p2"), now=0)
    state = p.dump()
    q = make()
    q.load(state)
    rows = {row["player"]: row["points"] for row in q.ranking()}
    assert rows == {"p1": 1016, "p2": 984}
    with pytest.raises(lbs.LadderError) as exc:
        q.settle("m1", ("s1", "p1"), ("s1", "p2"), now=1)
    assert exc.value.code == "already_settled"
