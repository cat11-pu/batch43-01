import pytest

import gathering_node_respawn as gnr


def make(**kwargs):
    return gnr.GatheringNodeRespawn(**kwargs)


def spawned(g, node="n1", line="l1", item="herb", position=(0, 0), now=0,
            rare=False, reachable=True):
    return g.spawn(node, line, item, position=position, now=now, rare=rare,
                   reachable=reachable)


def test_cooldown_blocks_immediate_second_gather():
    g = make(cooldown=300)
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=0)
    g.finish_gather("n1", "l1", "p1", now=0)
    with pytest.raises(gnr.GatherError) as exc:
        g.begin_gather("n1", "l1", "p1", now=100)
    assert exc.value.code == "NODE_COOLDOWN"
    assert g.availability("n1", "l1", now=299)["available"] is False
    assert g.availability("n1", "l1", now=300)["available"] is True


def test_cooldown_is_tracked_per_line():
    g = make(cooldown=300)
    spawned(g, line="l1")
    spawned(g, line="l2")
    g.begin_gather("n1", "l1", "p1", now=0)
    g.finish_gather("n1", "l1", "p1", now=0)
    assert g.begin_gather("n1", "l2", "p1", now=1)["gathering"] is True


def test_interrupted_gather_costs_no_durability_or_item():
    g = make()
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=0)
    out = g.interrupt_gather("n1", "l1", "p1", now=1)
    assert out["interrupted"] is True
    snap = g.snapshot("p1")
    assert snap["items"] == {}
    assert snap["durability_spent"] == 0


def test_two_players_cannot_gather_same_node():
    g = make()
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=0)
    with pytest.raises(gnr.GatherError) as exc:
        g.begin_gather("n1", "l1", "p2", now=1)
    assert exc.value.code == "NODE_BUSY"
    g.finish_gather("n1", "l1", "p1", now=2)
    assert g.snapshot("p1")["items"] == {"herb": 1}
    assert g.snapshot("p2")["items"] == {}


def test_maintenance_does_not_shift_respawn_clock():
    g = make(cooldown=300)
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=1000)
    g.finish_gather("n1", "l1", "p1", now=1000)
    g.set_maintenance(now=1100, duration=500)
    assert g.availability("n1", "l1", now=1299)["available"] is False
    assert g.availability("n1", "l1", now=1300)["available"] is True


def test_cooldown_starts_at_gather_time_not_server_open():
    g = make(cooldown=300)
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=1000)
    out = g.finish_gather("n1", "l1", "p1", now=1000)
    assert out["available_at"] == 1300
    assert g.availability("n1", "l1", now=1000)["available_at"] == 1300


def test_rare_node_cannot_be_displaced_by_common_spawn():
    g = make()
    spawned(g, node="rare", position=(5, 5), rare=True)
    assert [n["node_id"] for n in g.nodes("l1")] == ["rare"]
    with pytest.raises(gnr.GatherError) as exc:
        spawned(g, node="common", position=(5, 5), rare=False)
    assert exc.value.code == "POSITION_OCCUPIED"
    assert [n["node_id"] for n in g.nodes("l1")] == ["rare"]


def test_client_crit_is_ignored_and_server_crit_wins():
    g = make()
    spawned(g, node="n1", item="herb")
    spawned(g, node="n2", item="herb", position=(1, 1))
    g.begin_gather("n1", "l1", "p1", now=0)
    low = g.finish_gather("n1", "l1", "p1", now=0, client_crit=1.0,
                          server_crit=0.0)
    g.begin_gather("n2", "l1", "p1", now=0)
    high = g.finish_gather("n2", "l1", "p1", now=0, client_crit=0.0,
                           server_crit=0.9)
    assert low["quantity"] == 1
    assert high["quantity"] == 2


def test_level_requirement_blocks_gathering_without_item():
    g = make()
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=0)
    with pytest.raises(gnr.GatherError) as exc:
        g.finish_gather("n1", "l1", "p1", now=1, level=1, required_level=10)
    assert exc.value.code == "LEVEL_TOO_LOW"
    snap = g.snapshot("p1")
    assert snap["items"] == {}
    assert snap["durability_spent"] == 0


def test_unreachable_spawn_is_refused():
    g = make()
    with pytest.raises(gnr.GatherError) as exc:
        spawned(g, reachable=False)
    assert exc.value.code == "UNREACHABLE"
    assert g.nodes("l1") == []


def test_line_node_cap_is_enforced_per_line():
    g = make(node_cap_per_line=10)
    g.configure_line("l1", 1)
    spawned(g, node="n1", line="l1", position=(0, 0))
    with pytest.raises(gnr.GatherError) as exc:
        spawned(g, node="n2", line="l1", position=(1, 1))
    assert exc.value.code == "LINE_NODE_LIMIT"
    assert spawned(g, node="n3", line="l2", position=(1, 1))["line_id"] == "l2"


def test_task_counter_counts_finished_gathers_only():
    g = make()
    spawned(g, node="n1", position=(0, 0))
    spawned(g, node="n2", position=(1, 1))
    g.begin_gather("n1", "l1", "p1", now=0)
    g.finish_gather("n1", "l1", "p1", now=0)
    g.begin_gather("n2", "l1", "p1", now=1)
    g.interrupt_gather("n2", "l1", "p1", now=2)
    assert g.snapshot("p1")["tasks"]["gathering"] == 1


def test_single_gatherer_holds_the_node():
    g = make()
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=0)
    with pytest.raises(gnr.GatherError) as exc:
        g.begin_gather("n1", "l1", "p1", now=1)
    assert exc.value.code == "NODE_BUSY"
    assert g.availability("n1", "l1", now=1)["available"] is False


def test_inventory_failure_rolls_back_durability_and_cooldown():
    def broken_inventory(item_id, quantity):
        raise RuntimeError("bag service down")

    g = make(inventory=broken_inventory)
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=0)
    with pytest.raises(gnr.GatherError) as exc:
        g.finish_gather("n1", "l1", "p1", now=0, durability=2)
    assert exc.value.code == "GRANT_FAILED"
    snap = g.snapshot("p1")
    assert snap["durability_spent"] == 0
    assert snap["items"] == {}
    assert g.availability("n1", "l1", now=0)["available"] is True


def test_restart_restores_cooldown_deadline():
    g = make(cooldown=300)
    spawned(g)
    g.begin_gather("n1", "l1", "p1", now=0)
    g.finish_gather("n1", "l1", "p1", now=0)
    state = g.dump()
    restored = make(cooldown=300)
    restored.load(state)
    assert restored.availability("n1", "l1", now=100)["available"] is False
    assert restored.availability("n1", "l1", now=300)["available"] is True
