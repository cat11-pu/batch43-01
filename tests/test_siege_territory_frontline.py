import pytest

import siege_territory_frontline as stf


def make(**kwargs):
    return stf.SiegeTerritoryFrontline(**kwargs)


def siege(attackers=("atk1",), defenders=("def1",), owner="def1"):
    s = make()
    s.register_stronghold("h1", owner=owner)
    s.start_siege("h1", list(attackers), list(defenders),
                  window_start=0, window_end=100)
    return s


def test_no_damage_means_no_progress():
    s = siege()
    out = s.tick("h1", tick_id=1, damage_by_guild={}, now=10)
    assert out["progress"]["atk1"] == 0
    assert s.owner("h1") == "def1"


def test_two_full_progress_attackers_yield_one_owner():
    s = siege(attackers=("atk1", "atk2"))
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 100, "atk2": 100},
           now=50)
    out = s.end_window("h1", now=100)
    assert out["owner"] == "atk1"
    assert s.owner("h1") == "atk1"
    assert s.buff_affects("h1", "atk2") is False


def test_duplicate_tick_id_is_applied_once():
    s = siege()
    s.tick("h1", tick_id=7, damage_by_guild={"atk1": 30}, now=10)
    again = s.tick("h1", tick_id=7, damage_by_guild={"atk1": 30}, now=11)
    assert again["applied"] is False
    assert again["duplicate"] is True
    assert s.progress("h1")["progress"]["atk1"] == 30


def test_reward_reads_final_ownership_not_mid_snapshot():
    s = siege()
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 60}, now=10)
    s.tick("h1", tick_id=2, damage_by_guild={"def1": 20}, now=20)
    out = s.end_window("h1", now=100)
    assert out["owner"] == s.owner("h1") == "atk1"


def test_respawn_point_remains_available_when_contested():
    s = siege()
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 50}, now=10)
    out = s.respawn_point("h1", "def1")
    assert out["available"] is True
    assert out["stronghold_id"] == "h1"


def test_disbanding_owner_releases_territory():
    s = siege()
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 100}, now=10)
    s.end_window("h1", now=100)
    out = s.disband_guild("atk1")
    assert out["released"] is True
    assert s.owner("h1") is None


def test_end_before_window_end_is_refused():
    s = siege()
    with pytest.raises(stf.SiegeError) as exc:
        s.end_window("h1", now=99)
    assert exc.value.code == "WINDOW_NOT_ENDED"
    assert s.owner("h1") == "def1"


def test_damage_after_window_end_is_ignored():
    s = siege()
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 40}, now=10)
    s.end_window("h1", now=100)
    with pytest.raises(stf.SiegeError) as exc:
        s.tick("h1", tick_id=2, damage_by_guild={"atk1": 100}, now=101)
    assert exc.value.code == "WINDOW_CLOSED"
    assert s.progress("h1")["progress"]["atk1"] == 40


def test_guild_cannot_fight_on_both_sides():
    s = make()
    s.register_stronghold("h1", owner="def1")
    with pytest.raises(stf.SiegeError) as exc:
        s.start_siege("h1", ["g1"], ["g1"], window_start=0, window_end=100)
    assert exc.value.code == "SIDE_CONFLICT"


def test_buff_only_applies_to_owner_side():
    s = siege()
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 80}, now=10)
    s.end_window("h1", now=100)
    assert s.buff_affects("h1", "atk1") is True
    assert s.buff_affects("h1", "def1") is False


def test_restart_restores_progress_and_owner():
    s = siege()
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 70}, now=10)
    s.end_window("h1", now=100)
    state = s.dump()
    restored = make()
    restored.load(state)
    assert restored.owner("h1") == "atk1"
    assert restored.progress("h1")["progress"]["atk1"] == 70


def test_tie_is_broken_by_kills_and_world_kills_do_not_count():
    s = siege(attackers=("atk1", "atk2"))
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 50, "atk2": 50,
                                             "def1": 50}, now=10)
    s.record_kill("h1", "evt-1", "atk1")
    s.record_kill("h1", "evt-2", "atk2")
    s.record_kill("h1", "evt-3", "atk2")
    s.record_kill("h1", "evt-4", "atk1", source="world")
    out = s.end_window("h1", now=100)
    assert out["owner"] == "atk2"


def test_only_one_owner_exists_after_handover():
    s = siege(attackers=("atk1", "atk2"), defenders=("def1", "def2"))
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 90, "def2": 10},
           now=10)
    s.end_window("h1", now=100)
    winners = [g for g in ("atk1", "atk2", "def1", "def2")
               if s.buff_affects("h1", g)]
    assert winners == ["atk1"]


def test_same_tick_score_is_not_double_counted():
    s = siege()
    s.tick("h1", tick_id=3, damage_by_guild={"atk1": 20}, now=10)
    s.tick("h1", tick_id=3, damage_by_guild={"atk1": 20}, now=10)
    assert s.progress("h1")["progress"]["atk1"] == 20


def test_progress_never_goes_backwards():
    s = siege()
    s.tick("h1", tick_id=1, damage_by_guild={"atk1": 30}, now=10)
    first = s.progress("h1")["progress"]["atk1"]
    s.tick("h1", tick_id=2, damage_by_guild={"atk1": 5}, now=20)
    second = s.progress("h1")["progress"]["atk1"]
    s.tick("h1", tick_id=3, damage_by_guild={"atk1": 0}, now=30)
    third = s.progress("h1")["progress"]["atk1"]
    assert first <= second <= third
