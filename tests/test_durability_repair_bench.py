import pytest

import durability_repair_bench as drb


def make(**kwargs):
    return drb.DurabilityRepairBench(**kwargs)


def registered(bench, item="sword", owner="p1", maximum=100,
               durability=80, revision=1, set_id="default"):
    return bench.register_item(item, owner, maximum, durability,
                               revision=revision, set_id=set_id)


def test_repair_price_uses_missing_durability():
    b = make(rate_per_point=10)
    registered(b, durability=80)
    assert b.quote("sword")["cost"] == 200
    out = b.repair("sword", "p1", revision=1)
    assert out["durability"] == 100


def test_reduced_max_durability_is_respected():
    b = make()
    registered(b, maximum=100, durability=60)
    b.set_max_durability("sword", 80)
    out = b.repair("sword", "p1", revision=2)
    assert out["durability"] == 80
    assert b.snapshot("sword")["max_durability"] == 80


def test_broken_item_has_no_attributes():
    b = make()
    registered(b, durability=0)
    assert b.attributes("sword") == {}


def test_stale_repair_revision_is_charged_once():
    charges = []

    def wallet(amount):
        charges.append(amount)
        return True

    b = make(wallet=wallet)
    registered(b, durability=80)
    b.repair("sword", "p1", revision=1)
    with pytest.raises(drb.RepairError) as exc:
        b.repair("sword", "p1", revision=1)
    assert exc.value.code == "STALE_REVISION"
    assert len(charges) == 1


def test_discount_is_applied_exactly_once():
    charges = []

    def wallet(amount):
        charges.append(amount)
        return True

    b = make(rate_per_point=10, wallet=wallet)
    registered(b, durability=80)
    quote = b.quote("sword", discount=0.5)
    out = b.repair("sword", "p1", revision=1, discount=0.5)
    assert quote["cost"] == 100
    assert out["cost"] == 100
    assert charges == [100]


def test_trade_bumps_revision_and_old_request_fails():
    b = make()
    registered(b, owner="p1", durability=50)
    b.trade("sword", "p2")
    with pytest.raises(drb.RepairError) as exc:
        b.repair("sword", "p1", revision=1)
    assert exc.value.code in ("STALE_REVISION", "NOT_OWNER")
    out = b.repair("sword", "p2", revision=2)
    assert out["durability"] == 100


def test_broken_equipment_cannot_be_listed():
    b = make()
    registered(b, durability=0)
    with pytest.raises(drb.RepairError) as exc:
        b.list_for_auction("sword")
    assert exc.value.code == "ITEM_BROKEN"


def test_death_wear_after_repair_applies_again():
    b = make()
    registered(b, durability=20)
    b.repair("sword", "p1", revision=1)
    out = b.apply_death_wear("sword", amount=5)
    assert out["durability"] == 95
    assert b.snapshot("sword")["durability"] == 95


def test_quote_matches_amount_charged():
    charges = []

    def wallet(amount):
        charges.append(amount)
        return True

    b = make(rate_per_point=7, wallet=wallet)
    registered(b, durability=42)
    quote = b.quote("sword")
    b.repair("sword", "p1", revision=1)
    assert charges == [quote["cost"]]


def test_repair_in_combat_is_refused():
    b = make()
    registered(b, durability=40)
    with pytest.raises(drb.RepairError) as exc:
        b.repair("sword", "p1", revision=1, in_combat=True)
    assert exc.value.code == "IN_COMBAT"
    assert b.snapshot("sword")["durability"] == 40


def test_wallet_failure_rolls_back_durability():
    b = make(wallet=lambda amount: False)
    registered(b, durability=40)
    with pytest.raises(drb.RepairError) as exc:
        b.repair("sword", "p1", revision=1)
    assert exc.value.code == "PAYMENT_FAILED"
    snap = b.snapshot("sword")
    assert snap["durability"] == 40
    assert snap["revision"] == 1


def test_repair_all_only_touches_selected_set():
    b = make()
    registered(b, item="sword", durability=50, set_id="pvp")
    registered(b, item="helm", durability=50, set_id="pvp")
    registered(b, item="boots", durability=50, set_id="pve")
    out = b.repair_all("p1", set_id="pvp")
    assert out["repaired"] == 2
    assert b.snapshot("boots")["durability"] == 50


def test_each_charge_corresponds_to_one_restoration():
    charges = []

    def wallet(amount):
        charges.append(amount)
        return True

    b = make(wallet=wallet)
    registered(b, item="sword", durability=90)
    registered(b, item="helm", durability=70)
    b.repair("sword", "p1", revision=1)
    b.repair("helm", "p1", revision=1)
    assert len(charges) == 2
    assert b.snapshot("sword")["durability"] == 100
    assert b.snapshot("helm")["durability"] == 100


def test_durability_never_exceeds_current_max():
    b = make()
    registered(b, maximum=100, durability=90)
    b.set_max_durability("sword", 70)
    b.repair("sword", "p1", revision=2)
    snap = b.snapshot("sword")
    assert snap["durability"] <= snap["max_durability"]
    assert snap["durability"] == 70


def test_restart_restores_durability_and_revision():
    b = make()
    registered(b, durability=60)
    b.repair("sword", "p1", revision=1)
    state = b.dump()
    restored = make()
    restored.load(state)
    snap = restored.snapshot("sword")
    assert snap["durability"] == 100
    assert snap["revision"] == 2
    with pytest.raises(drb.RepairError):
        restored.repair("sword", "p1", revision=1)
