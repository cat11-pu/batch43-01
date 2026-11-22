import pytest

import boss_contribution_split as bcs


def make(**kwargs):
    return bcs.BossContributionSplit(**kwargs)


def fight(p, boss="world-boss", shard="s1"):
    return p.start_fight(boss, shard=shard)["battle_id"]


def test_each_fight_gets_a_unique_battle_id_and_reuses_active_one():
    p = make()
    first = fight(p)
    again = p.start_fight("world-boss", shard="s1")
    assert again["status"] == "existing"
    assert again["battle_id"] == first
    other_shard = fight(p, shard="s2")
    assert other_shard != first
    p.finish(first)
    p.settle(first)
    fresh = fight(p)
    assert fresh not in (first, other_shard)


def test_pet_damage_is_counted_once_for_the_owner():
    p = make()
    b = fight(p)
    p.join(b, "hunter")
    p.record_damage(b, "hunter", 100, pet=True)
    p.record_damage(b, "hunter", 100, pet=True)
    p.record_damage(b, "hunter", 100)
    assert p.contributions(b)["hunter"]["damage"] == 300


def test_weighted_contribution_formula():
    p = make(weights={"damage": 1.0, "heal": 0.5, "mitigation": 0.25})
    b = fight(p)
    p.join(b, "tank")
    p.record_damage(b, "tank", 100)
    p.record_heal(b, "tank", 200)
    p.record_mitigation(b, "tank", 400)
    row = p.contributions(b)["tank"]
    assert row["damage"] == 100
    assert row["heal"] == 200
    assert row["mitigation"] == 400
    assert row["contribution"] == 300


def test_records_after_finish_are_rejected():
    p = make()
    b = fight(p)
    p.join(b, "dps")
    p.finish(b)
    with pytest.raises(bcs.BossError) as exc:
        p.record_damage(b, "dps", 50)
    assert exc.value.code == "fight_ended"
    assert p.contributions(b)["dps"]["damage"] == 0


def test_finish_is_idempotent():
    p = make()
    b = fight(p)
    p.join(b, "dps")
    p.record_damage(b, "dps", 80)
    first = p.finish(b)
    second = p.finish(b)
    assert first["status"] == "finished"
    assert second["status"] == "already_finished"
    assert p.contributions(b)["dps"]["damage"] == 80


def test_settle_before_finish_is_rejected():
    p = make()
    b = fight(p)
    p.join(b, "dps")
    p.record_damage(b, "dps", 100)
    with pytest.raises(bcs.BossError) as exc:
        p.settle(b)
    assert exc.value.code == "fight_active"
    assert p.rewards(b) == []


def test_settling_twice_does_not_mail_twice():
    p = make()
    b = fight(p)
    p.join(b, "dps")
    p.record_damage(b, "dps", 100)
    p.finish(b)
    p.settle(b)
    with pytest.raises(bcs.BossError) as exc:
        p.settle(b)
    assert exc.value.code == "already_settled"
    assert len(p.mailbox("dps")) == 1


def test_last_hit_does_not_take_the_whole_pool():
    p = make(reward_pool=1000)
    b = fight(p)
    p.join(b, "carry")
    p.join(b, "finisher")
    p.record_damage(b, "carry", 900)
    p.record_damage(b, "finisher", 100)
    p.record_kill(b, "finisher")
    p.finish(b)
    p.settle(b)
    rewards = {row["player"]: row["reward"] for row in p.rewards(b)}
    assert rewards == {"carry": 900, "finisher": 100}


def test_disconnect_keeps_contribution_until_the_end():
    p = make(reward_pool=1000)
    b = fight(p)
    p.join(b, "dc-player", server="s2")
    p.record_damage(b, "dc-player", 500)
    p.disconnect(b, "dc-player")
    with pytest.raises(bcs.BossError) as exc:
        p.record_damage(b, "dc-player", 10)
    assert exc.value.code == "player_offline"
    p.finish(b)
    p.settle(b)
    assert p.contributions(b)["dc-player"]["damage"] == 500
    assert p.mailbox("dc-player")[0]["reward"] == 1000


def test_joined_player_with_no_actions_gets_no_reward():
    p = make()
    b = fight(p)
    p.join(b, "afk")
    p.join(b, "dps")
    p.record_damage(b, "dps", 300)
    p.finish(b)
    p.settle(b)
    assert [row["player"] for row in p.rewards(b)] == ["dps"]
    assert [row["player"] for row in p.leaderboard(b)] == ["dps"]
    assert p.mailbox("afk") == []


def test_new_fight_resets_contribution_ledger():
    p = make()
    first = fight(p)
    p.join(first, "dps")
    p.record_damage(first, "dps", 400)
    p.finish(first)
    p.settle(first)
    second = fight(p)
    assert second != first
    assert p.contributions(second) == {}
    p.join(second, "dps")
    p.record_damage(second, "dps", 10)
    assert p.contributions(second)["dps"]["damage"] == 10


def test_cross_server_reward_is_mailed_on_home_server():
    p = make()
    b = fight(p)
    p.join(b, "cross", server="s2")
    p.record_damage(b, "cross", 100)
    p.finish(b)
    p.settle(b)
    mail = p.mailbox("cross")[0]
    assert mail["server"] == "s2"
    assert mail["battle_id"] == b


def test_mail_damage_matches_leaderboard_damage():
    p = make()
    b = fight(p)
    p.join(b, "dps")
    p.record_damage(b, "dps", 777)
    p.record_heal(b, "dps", 100)
    p.finish(b)
    p.settle(b)
    board = {row["player"]: row for row in p.leaderboard(b)}
    mail = p.mailbox("dps")[0]
    assert mail["damage"] == board["dps"]["damage"] == 777
    assert mail["contribution"] == board["dps"]["contribution"]


def test_reward_shares_sum_to_the_whole_pool():
    p = make(reward_pool=1000)
    b = fight(p)
    for player, damage in (("a", 333), ("b", 333), ("c", 334)):
        p.join(b, player)
        p.record_damage(b, player, damage)
    p.finish(b)
    p.settle(b)
    rewards = [row["reward"] for row in p.rewards(b)]
    assert sum(rewards) == 1000
    assert all(abs(value - 333) <= 1 for value in rewards)


def test_healer_with_zero_damage_is_still_eligible():
    p = make(reward_pool=1000)
    b = fight(p)
    p.join(b, "healer")
    p.join(b, "dps")
    p.record_heal(b, "healer", 400)
    p.record_damage(b, "dps", 200)
    p.finish(b)
    p.settle(b)
    rewards = {row["player"]: row["reward"] for row in p.rewards(b)}
    assert rewards == {"healer": 500, "dps": 500}
