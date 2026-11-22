import pytest

import talent_respec_binder as trb


def make(**kwargs):
    return trb.TalentRespecBinder(**kwargs)


def test_points_are_conserved_after_spending():
    b = make()
    b.grant_points("hero", 10)
    b.spend_point("hero", "attack", 3)
    snap = b.snapshot("hero")
    assert snap["total"] == 10
    assert snap["spent"] == 3
    assert snap["remaining"] == 7
    assert snap["spent"] + snap["remaining"] == snap["total"]


def test_respec_refunds_all_points_including_task_sources():
    b = make()
    b.grant_points("hero", 10)
    b.grant_points("hero", 2, source="task")
    b.spend_point("hero", "attack", 3)
    b.spend_point("hero", "defense", 2)
    out = b.respec("hero")
    assert out["refunded"] == 5
    snap = b.snapshot("hero")
    assert snap["spent"] == 0
    assert snap["remaining"] == 12


def test_spending_more_than_available_is_rejected():
    b = make()
    b.grant_points("hero", 3)
    with pytest.raises(trb.TalentError) as exc:
        b.spend_point("hero", "attack", 5)
    assert exc.value.code == "INSUFFICIENT_POINTS"
    snap = b.snapshot("hero")
    assert snap["spent"] == 0
    assert snap["remaining"] == 3


def test_negative_point_spend_is_rejected():
    b = make()
    b.grant_points("hero", 10)
    with pytest.raises(trb.TalentError) as exc:
        b.spend_point("hero", "attack", -2)
    assert exc.value.code == "INVALID_POINTS"
    assert b.snapshot("hero")["remaining"] == 10


def test_switching_scheme_does_not_stack_passives():
    b = make()
    b.grant_points("hero", 10)
    b.save_scheme("hero", "fire", {"passive_fire": 1, "attack": 3},
                  revision=1)
    b.save_scheme("hero", "ice", {"passive_ice": 1, "attack": 2},
                  revision=1)
    b.activate_scheme("hero", "fire", now=0)
    b.activate_scheme("hero", "ice", now=20)
    snap = b.snapshot("hero")
    assert snap["active_scheme"] == "ice"
    assert snap["passives"] == ["passive_ice"]
    assert "passive_fire" not in snap["passives"]


def test_switch_cooldown_blocks_rapid_switch():
    b = make(switch_cooldown=10)
    b.grant_points("hero", 10)
    b.save_scheme("hero", "fire", {"attack": 3}, revision=1)
    b.save_scheme("hero", "ice", {"attack": 2}, revision=1)
    b.activate_scheme("hero", "fire", now=0)
    with pytest.raises(trb.TalentError) as exc:
        b.activate_scheme("hero", "ice", now=5)
    assert exc.value.code == "SWITCH_COOLDOWN"
    assert b.snapshot("hero")["active_scheme"] == "fire"
    out = b.activate_scheme("hero", "ice", now=10)
    assert out["active_scheme"] == "ice"


def test_respec_cost_uses_highest_level_reached():
    b = make(cost_per_level=100)
    b.set_level("hero", 20)
    b.set_level("hero", 10)
    b.grant_points("hero", 10)
    b.spend_point("hero", "attack", 3)
    out = b.respec("hero")
    assert out["cost"] == 2000
    assert out["refunded"] == 3


def test_unequip_removes_bonus_points():
    b = make()
    b.grant_points("hero", 10)
    b.equip_bonus("hero", "relic", 2)
    assert b.snapshot("hero")["total"] == 12
    b.unequip_bonus("hero", "relic")
    snap = b.snapshot("hero")
    assert snap["total"] == 10
    assert snap["remaining"] == 10


def test_scheme_import_rejects_unknown_version_without_zeroing():
    b = make()
    b.grant_points("hero", 10)
    with pytest.raises(trb.TalentError) as exc:
        b.import_scheme("hero", "old",
                        {"version": 1, "allocation": {"attack": 3}})
    assert exc.value.code == "SCHEME_VERSION_MISMATCH"
    snap = b.snapshot("hero")
    assert snap["remaining"] == 10
    assert snap["active_scheme"] is None


def test_stale_scheme_save_is_rejected():
    b = make()
    b.grant_points("hero", 10)
    b.save_scheme("hero", "fire", {"attack": 3}, revision=1,
                  base_revision=0)
    with pytest.raises(trb.TalentError) as exc:
        b.save_scheme("hero", "fire", {"attack": 1}, revision=2,
                      base_revision=0)
    assert exc.value.code == "REVISION_CONFLICT"
    preview = b.preview("hero", "fire")
    assert preview["allocation"] == {"attack": 3}


def test_respec_returns_task_talent_book():
    b = make()
    b.grant_points("hero", 10)
    b.grant_points("hero", 2, source="task", item="talent_book")
    b.spend_point("hero", "attack", 2)
    out = b.respec("hero")
    assert out["items_returned"] == ["talent_book"]
    assert b.snapshot("hero")["items"] == []


def test_preview_does_not_change_active_scheme():
    b = make()
    b.grant_points("hero", 10)
    b.save_scheme("hero", "fire", {"passive_fire": 1, "attack": 3},
                  revision=1)
    b.save_scheme("hero", "ice", {"passive_ice": 1, "attack": 2},
                  revision=1)
    b.activate_scheme("hero", "fire", now=0)
    preview = b.preview("hero", "ice")
    assert preview["passives"] == ["passive_ice"]
    snap = b.snapshot("hero")
    assert snap["active_scheme"] == "fire"
    assert snap["passives"] == ["passive_fire"]


def test_failed_wallet_rolls_back_respec():
    b = make(wallet=lambda amount: False)
    b.grant_points("hero", 10)
    b.spend_point("hero", "attack", 3)
    with pytest.raises(trb.TalentError) as exc:
        b.respec("hero")
    assert exc.value.code == "RESPEC_ROLLBACK"
    snap = b.snapshot("hero")
    assert snap["spent"] == 3
    assert snap["remaining"] == 7


def test_points_never_negative_after_sequence():
    b = make()
    b.grant_points("hero", 5)
    b.spend_point("hero", "attack", 3)
    with pytest.raises(trb.TalentError):
        b.spend_point("hero", "attack", 5)
    b.spend_point("hero", "attack", 2)
    with pytest.raises(trb.TalentError):
        b.spend_point("hero", "attack", 1)
    snap = b.snapshot("hero")
    assert snap["remaining"] == 0
    assert snap["spent"] == 5
    assert snap["remaining"] >= 0


def test_only_one_scheme_active_after_switch():
    b = make()
    b.grant_points("hero", 10)
    b.save_scheme("hero", "fire", {"passive_fire": 1}, revision=1)
    b.save_scheme("hero", "ice", {"passive_ice": 1}, revision=1)
    b.activate_scheme("hero", "fire", now=0)
    b.activate_scheme("hero", "ice", now=20)
    snap = b.snapshot("hero")
    assert snap["active_scheme"] == "ice"
    assert "passive_fire" not in snap["passives"]
    assert len(snap["passives"]) == 1
