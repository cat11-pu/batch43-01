import pytest

import quest_chain_prereq as qcp


def make():
    return qcp.QuestChainPrereq()


def test_direct_prerequisite_must_be_completed():
    p = make()
    p.define_quest("q1")
    p.define_quest("q2", prereqs=["q1"])
    with pytest.raises(qcp.QuestError) as exc:
        p.accept("char-1", "q2")
    assert exc.value.code == "prereq_incomplete"
    assert p.tracker("char-1") == []


def test_imported_completion_does_not_bypass_transitive_chain():
    p = make()
    p.define_quest("leaf")
    p.define_quest("mid", prereqs=["leaf"])
    p.define_quest("top", prereqs=["mid"])
    p.import_completion("char-1", ["mid"])
    with pytest.raises(qcp.QuestError) as exc:
        p.accept("char-1", "top")
    assert exc.value.code == "prereq_incomplete"


def test_full_chain_unlocks_after_each_link_completes():
    p = make()
    p.define_quest("leaf")
    p.define_quest("mid", prereqs=["leaf"])
    p.define_quest("top", prereqs=["mid"])
    for quest in ("leaf", "mid", "top"):
        p.accept("char-1", quest)
        p.complete("char-1", quest)
    assert [row["quest_id"] for row in p.tracker("char-1")] == []
    assert p.quest_state("char-1", "top")["status"] == "completed"


def test_duplicate_accept_returns_existing_instance_and_grants_once():
    p = make()
    p.define_quest("q1", grant_on_accept={"scroll": 1})
    first = p.accept("char-1", "q1")
    second = p.accept("char-1", "q1")
    assert second["status"] == "already_active"
    assert second["instance_id"] == first["instance_id"]
    assert p.inventory("char-1") == {"scroll": 1}


def test_abandon_recovers_starter_and_collected_items():
    p = make()
    p.define_quest("q1", grant_on_accept={"scroll": 1},
                   items_required={"pelt": 2})
    p.accept("char-1", "q1")
    p.add_item("char-1", "pelt", 3)
    out = p.abandon("char-1", "q1")
    assert out["status"] == "abandoned"
    assert p.inventory("char-1") == {}
    assert p.tracker("char-1") == []


def test_failed_quest_also_recovers_quest_items():
    p = make()
    p.define_quest("q1", items_required={"pelt": 1})
    p.accept("char-1", "q1")
    p.add_item("char-1", "pelt", 1)
    out = p.fail("char-1", "q1")
    assert out["status"] == "failed"
    assert p.inventory("char-1") == {}


def test_completion_is_idempotent_and_rewards_once():
    p = make()
    p.define_quest("q1", rewards={"gold": 10})
    p.accept("char-1", "q1")
    assert p.complete("char-1", "q1")["status"] == "completed"
    again = p.complete("char-1", "q1")
    assert again["status"] == "already_completed"
    assert len(p.mailbox("char-1")) == 1
    assert p.mailbox("char-1")[0]["gold"] == 10


def test_completion_without_required_items_keeps_quest_active():
    p = make()
    p.define_quest("q1", items_required={"pelt": 1}, rewards={"gold": 5})
    p.accept("char-1", "q1")
    with pytest.raises(qcp.QuestError) as exc:
        p.complete("char-1", "q1")
    assert exc.value.code == "requirements_unmet"
    assert p.quest_state("char-1", "q1")["status"] == "active"
    assert p.mailbox("char-1") == []


def test_party_progress_is_shared_with_listed_members():
    p = make()
    p.define_quest("q1", objective={"kills": 2})
    p.accept("char-a", "q1")
    p.accept("char-b", "q1")
    p.progress("char-a", "q1", amount=1, objective="kills",
               party=["char-a", "char-b"])
    assert p.quest_state("char-a", "q1")["counters"]["kills"] == 1
    assert p.quest_state("char-b", "q1")["counters"]["kills"] == 1


def test_player_who_left_party_stops_earning_credit():
    p = make()
    p.define_quest("q1", objective={"kills": 5})
    p.accept("char-a", "q1")
    p.accept("char-b", "q1")
    p.progress("char-a", "q1", amount=1, objective="kills",
               party=["char-a", "char-b"])
    p.progress("char-a", "q1", amount=2, objective="kills", party=["char-a"])
    assert p.quest_state("char-a", "q1")["counters"]["kills"] == 3
    assert p.quest_state("char-b", "q1")["counters"]["kills"] == 1


def test_quest_items_are_bound_and_cannot_be_traded():
    p = make()
    p.define_quest("q1", items_required={"pelt": 1})
    p.accept("char-1", "q1")
    p.add_item("char-1", "pelt", 1)
    with pytest.raises(qcp.QuestError) as exc:
        p.transfer_item("char-1", "char-2", "pelt", 1)
    assert exc.value.code == "item_bound"
    assert p.inventory("char-1") == {"pelt": 1}
    assert p.inventory("char-2") == {}


def test_daily_and_main_quests_have_separate_counters():
    p = make()
    p.define_quest("main", objective={"kills": 5})
    p.define_quest("daily", objective={"kills": 1})
    p.accept("char-1", "main")
    p.accept("char-1", "daily")
    p.progress("char-1", "daily", amount=1, objective="kills")
    assert p.quest_state("char-1", "daily")["counters"]["kills"] == 1
    assert p.quest_state("char-1", "main")["counters"]["kills"] == 0


def test_faction_locked_quest_rejected_until_faction_matches():
    p = make()
    p.define_quest("ally-only", faction="alliance")
    p.set_faction("char-1", "horde")
    with pytest.raises(qcp.QuestError) as exc:
        p.accept("char-1", "ally-only")
    assert exc.value.code == "faction_locked"
    p.set_faction("char-1", "alliance")
    assert p.accept("char-1", "ally-only")["status"] == "active"


def test_old_quest_id_alias_satisfies_new_prerequisite():
    p = make()
    p.define_quest("q1", aliases=["q1_v1"])
    p.define_quest("q2", prereqs=["q1"])
    p.accept("char-1", "q1_v1")
    p.complete("char-1", "q1_v1")
    assert p.quest_state("char-1", "q1")["status"] == "completed"
    assert p.accept("char-1", "q2")["status"] == "active"


def test_restore_keeps_instance_counters_and_reward_idempotency():
    p = make()
    p.define_quest("q1", objective={"kills": 2}, rewards={"gold": 7})
    p.accept("char-1", "q1")
    p.progress("char-1", "q1", amount=1, objective="kills")
    state = p.snapshot()
    q = make()
    q.define_quest("q1", objective={"kills": 2}, rewards={"gold": 7})
    q.restore(state)
    assert q.quest_state("char-1", "q1")["counters"]["kills"] == 1
    q.progress("char-1", "q1", amount=1, objective="kills")
    q.complete("char-1", "q1")
    assert q.complete("char-1", "q1")["status"] == "already_completed"
    assert len(q.mailbox("char-1")) == 1
