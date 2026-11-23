import pytest

import dungeon_room_allocator as dra


def make(**kwargs):
    kwargs.setdefault("min_party_size", 5)
    kwargs.setdefault("room_capacity", 5)
    return dra.DungeonRoomAllocator(**kwargs)


def queued(a, party, size=5, leader=None, now=0, difficulty="normal"):
    leader = leader or party + "-leader"
    return a.enqueue(party, size, leader=leader, now=now,
                     difficulty=difficulty)


def test_recycled_room_has_no_previous_progress_or_loot():
    a = make()
    queued(a, "p1")
    room = a.allocate(["p1"], now=0)
    a.record_boss(room["room_id"], "normal", "boss-1")
    a.grant_reward(room["room_id"], "normal", "loot-1")
    a.leave("p1")
    a.recycle_room(room["room_id"])
    assert a.progress(room["room_id"], "normal")["bosses"] == []
    assert a.progress(room["room_id"], "normal")["rewards"] == []


def test_four_player_party_cannot_start_five_player_dungeon():
    a = make()
    queued(a, "p1", size=4)
    with pytest.raises(dra.DungeonError) as exc:
        a.allocate(["p1"], now=0)
    assert exc.value.code == "PARTY_TOO_SMALL"
    assert a.queue_position("p1") == 1


def test_backfill_into_completed_instance_is_refused():
    a = make()
    queued(a, "p1")
    queued(a, "p2")
    room = a.allocate(["p1"], now=0)
    a.complete_instance(room["room_id"], "normal")
    with pytest.raises(dra.DungeonError) as exc:
        a.backfill(room["room_id"], "p2", "normal")
    assert exc.value.code == "INSTANCE_COMPLETED"


def test_double_click_creates_only_one_instance_and_one_reward():
    a = make()
    queued(a, "p1")
    first = a.allocate(["p1"], now=0)
    with pytest.raises(dra.DungeonError) as exc:
        a.allocate(["p1"], now=1)
    assert exc.value.code == "ALREADY_IN_INSTANCE"
    a.grant_reward(first["room_id"], "normal", "clear-reward")
    assert a.progress(first["room_id"], "normal")["rewards"] == ["clear-reward"]


def test_normal_and_heroic_progress_are_isolated():
    a = make()
    queued(a, "p1", difficulty="heroic")
    room = a.allocate(["p1"], difficulty="heroic", now=0)
    a.record_boss(room["room_id"], "heroic", "heroic-boss")
    assert a.progress(room["room_id"], "heroic")["bosses"] == ["heroic-boss"]
    assert a.progress(room["room_id"], "normal")["bosses"] == []


def test_reset_swaps_instance_id_and_clears_progress():
    a = make()
    queued(a, "p1")
    room = a.allocate(["p1"], now=0)
    a.record_boss(room["room_id"], "normal", "boss-1")
    old_instance = a.progress(room["room_id"], "normal")["instance_id"]
    reset = a.reset_instance(room["room_id"], "normal")
    assert reset["instance_id"] != old_instance
    assert a.progress(room["room_id"], "normal")["bosses"] == []


def test_occupied_room_cannot_be_recycled():
    a = make()
    queued(a, "p1")
    room = a.allocate(["p1"], now=0)
    with pytest.raises(dra.DungeonError) as exc:
        a.recycle_room(room["room_id"])
    assert exc.value.code == "ROOM_OCCUPIED"
    a.leave("p1")
    assert a.recycle_room(room["room_id"])["recycled"] is True


def test_unknown_party_in_batch_does_not_consume_other_parties():
    a = make()
    queued(a, "p1")
    with pytest.raises(dra.DungeonError) as exc:
        a.allocate(["p1", "ghost"], now=0)
    assert exc.value.code == "NOT_QUEUED"
    assert a.queue_position("p1") == 1


def test_room_factory_failure_requeues_party_at_original_position():
    def broken_factory(room_id):
        raise RuntimeError("instance service down")

    a = make(room_factory=broken_factory)
    queued(a, "p1")
    with pytest.raises(dra.DungeonError) as exc:
        a.allocate(["p1"], now=0)
    assert exc.value.code == "ROOM_CREATE_FAILED"
    assert a.queue_position("p1") == 1


def test_leader_transfer_takes_effect_for_entry():
    a = make()
    queued(a, "p1", leader="alice")
    room = a.allocate(["p1"], now=0)
    a.transfer_leader("p1", "bob")
    with pytest.raises(dra.DungeonError) as exc:
        a.enter("p1", "alice", room["room_id"])
    assert exc.value.code == "NOT_LEADER"
    assert a.enter("p1", "bob", room["room_id"])["entered"] is True


def test_reset_conflict_keeps_current_instance():
    a = make()
    queued(a, "p1")
    room = a.allocate(["p1"], now=0)
    old_instance = a.progress(room["room_id"], "normal")["instance_id"]
    a.reset_instance(room["room_id"], "normal")
    with pytest.raises(dra.DungeonError) as exc:
        a.reset_instance(room["room_id"], "normal",
                         expect_instance=old_instance)
    assert exc.value.code == "RESET_CONFLICT"
    assert a.progress(room["room_id"], "normal")["instance_id"] != old_instance


def test_restart_restores_rooms_and_queue():
    a = make()
    queued(a, "p1")
    queued(a, "p2")
    room = a.allocate(["p1"], now=0)
    a.record_boss(room["room_id"], "normal", "boss-1")
    state = a.dump()
    b = make()
    b.load(state)
    assert b.progress(room["room_id"], "normal")["bosses"] == ["boss-1"]
    assert b.queue_position("p2") == 2


def test_party_cannot_occupy_two_instances():
    a = make()
    queued(a, "p1")
    a.allocate(["p1"], now=0)
    with pytest.raises(dra.DungeonError):
        a.allocate(["p1"], now=1)


def test_room_id_is_not_reused_while_occupied():
    a = make()
    queued(a, "p1")
    queued(a, "p2")
    first = a.allocate(["p1"], now=0)
    second = a.allocate(["p2"], now=1)
    assert second["room_id"] != first["room_id"]


def test_room_creation_is_atomic_on_failure():
    calls = {"count": 0}

    def flaky_factory(room_id):
        calls["count"] += 1
        raise RuntimeError("boom")

    a = make(room_factory=flaky_factory)
    queued(a, "p1")
    with pytest.raises(dra.DungeonError):
        a.allocate(["p1"], now=0)
    assert calls["count"] == 1
    assert a.queue_position("p1") == 1
