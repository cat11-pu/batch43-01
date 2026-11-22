import pytest

import loot_roll_tally as lrt


def make(**kwargs):
    return lrt.LootRollTally(**kwargs)


def opened(t, drop="d1", item="epic-helm", deadline=100,
           eligible=("alice", "bob"), mode="need_greed"):
    return t.open_roll(drop, item, mode=mode, deadline=deadline,
                       eligible=eligible)


def test_offline_player_cannot_win_after_deadline():
    t = make()
    opened(t)
    t.vote("d1", "alice", 10, now=5)
    result = t.close_roll("d1", now=100)
    assert result["winner"] == "alice"
    with pytest.raises(lrt.LootError) as exc:
        t.vote("d1", "bob", 99, now=101)
    assert exc.value.code == "ROLL_CLOSED"


def test_tied_rolls_have_one_deterministic_winner():
    t = make()
    opened(t, eligible=("bob", "alice"))
    t.vote("d1", "bob", 50, now=1)
    t.vote("d1", "alice", 50, now=2)
    result = t.close_roll("d1", now=100)
    assert result["winner"] == "alice"
    assert result["tie"] is True
    assert t.get_roll("d1")["winner"] == "alice"


def test_paused_roll_refuses_settlement():
    t = make()
    opened(t)
    t.vote("d1", "alice", 50, now=1)
    t.pause("d1")
    with pytest.raises(lrt.LootError) as exc:
        t.close_roll("d1", now=100)
    assert exc.value.code == "ROLL_PAUSED"
    t.resume("d1", now=100)
    assert t.close_roll("d1", now=100)["winner"] == "alice"


def test_manual_delivery_to_departed_player_is_rejected():
    t = make()
    opened(t)
    t.vote("d1", "alice", 99, now=1)
    t.vote("d1", "bob", 10, now=2)
    t.close_roll("d1", now=100)
    with pytest.raises(lrt.LootError) as exc:
        t.deliver("d1", character="zoe")
    assert exc.value.code == "PLAYER_NOT_ELIGIBLE"
    assert t.outbox() == []
    assert t.get_roll("d1")["delivered_to"] is None


def test_need_beats_greed_even_with_lower_roll():
    t = make()
    opened(t, eligible=[{"id": "alice", "can_need": True},
                        {"id": "bob", "can_need": True}])
    t.vote("d1", "alice", 99, kind="greed", now=1)
    t.vote("d1", "bob", 5, kind="need", now=2)
    result = t.close_roll("d1", now=100)
    assert result["winner"] == "bob"


def test_reopening_settled_drop_keeps_original_result():
    t = make()
    opened(t)
    t.vote("d1", "alice", 99, now=1)
    first = t.close_roll("d1", now=100)
    again = t.open_roll("d1", "epic-helm", deadline=200,
                        eligible=("alice", "bob"), now=150)
    assert again["status"] == "settled"
    assert again["winner"] == first["winner"]
    assert len(t.get_roll("d1")["votes"]) == 1


def test_free_for_all_mode_does_not_deliver_roll_winner():
    t = make()
    opened(t, mode="free_for_all")
    result = t.close_roll("d1", now=100)
    assert result["winner"] is None
    first = t.loot_item("d1", "alice")
    second = t.loot_item("d1", "alice")
    assert first["looted"] is True
    assert second["looted"] is False
    assert t.outbox() == []


def test_reconnected_voter_does_not_double_bonus():
    t = make()
    opened(t)
    t.vote("d1", "alice", 50, bonus=5, now=1)
    with pytest.raises(lrt.LootError) as exc:
        t.vote("d1", "alice", 50, bonus=5, now=2)
    assert exc.value.code == "ALREADY_VOTED"
    roll = t.get_roll("d1")
    assert len(roll["votes"]) == 1
    assert roll["votes"][0]["total"] == 55


def test_rule_change_only_affects_next_round():
    t = make()
    opened(t)
    t.vote("d1", "alice", 99, now=1)
    t.close_roll("d1", now=100)
    t.set_rule("captain")
    assert t.get_roll("d1")["winner"] == "alice"
    opened(t, drop="d2", deadline=200, mode="captain")
    t.assign("d2", "bob")
    out = t.deliver("d2")
    assert out["delivered"] is True
    assert t.get_roll("d2")["delivered_to"] == "bob"


def test_cross_server_rolls_are_keyed_by_character_id():
    t = make()
    opened(t, eligible=("s1:alice", "s2:alice"))
    t.vote("d1", "s1:alice", 10, now=1)
    t.vote("d1", "s2:alice", 90, now=2)
    roll = t.get_roll("d1")
    assert sorted(v["character"] for v in roll["votes"]) == ["s1:alice",
                                                            "s2:alice"]
    assert t.close_roll("d1", now=100)["winner"] == "s2:alice"


def test_full_bag_falls_back_to_mail():
    t = make()
    opened(t)
    t.vote("d1", "alice", 99, now=1)
    t.close_roll("d1", now=100)
    out = t.deliver("d1", free_slots=0)
    assert out["delivered"] is False
    assert out["mailed"] is True
    assert len(t.outbox()) == 1
    assert t.outbox()[0]["character"] == "alice"
    assert t.outbox()[0]["item_id"] == "epic-helm"


def test_delivery_is_idempotent():
    t = make()
    opened(t)
    t.vote("d1", "alice", 99, now=1)
    t.close_roll("d1", now=100)
    first = t.deliver("d1")
    second = t.deliver("d1")
    assert first["delivered"] is True
    assert second["delivered"] is False
    assert second["duplicate"] is True
    assert t.get_roll("d1")["deliveries"] == 1


def test_need_vote_for_unusable_item_is_refused():
    t = make()
    opened(t, eligible=[{"id": "alice", "can_need": False},
                        {"id": "bob", "can_need": True}])
    with pytest.raises(lrt.LootError) as exc:
        t.vote("d1", "alice", 80, kind="need", now=1)
    assert exc.value.code == "CANNOT_NEED"
    assert t.get_roll("d1")["votes"] == []


def test_repeated_close_settles_once():
    t = make()
    opened(t)
    t.vote("d1", "alice", 99, now=1)
    first = t.close_roll("d1", now=100)
    second = t.close_roll("d1", now=200)
    assert second["winner"] == first["winner"]
    t.deliver("d1")
    assert t.get_roll("d1")["deliveries"] == 1


def test_rolls_are_scoped_to_one_drop():
    t = make()
    opened(t, drop="d1", deadline=100)
    opened(t, drop="d2", deadline=100)
    t.vote("d1", "alice", 99, now=1)
    roll = t.get_roll("d2")
    assert roll["votes"] == []
    assert t.close_roll("d2", now=100)["winner"] is None
    assert t.get_roll("d1")["winner"] is None
