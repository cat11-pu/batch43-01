import pytest

import set_bonus_aggregator as sba


def item(key, name=None, set_id="dragon"):
    return {"item_id": key, "name": name or key, "set_id": set_id}


DRAGON = {2: {"strength": 10}, 4: {"strength": 30}}


def make():
    p = sba.SetBonusAggregator()
    p.define_set("dragon", DRAGON)
    return p


def equip_four(p, set_id="dragon"):
    for index in range(4):
        p.equip("slot-%d" % index, item("i%d" % index, set_id=set_id))


def test_same_item_in_two_slots_counts_once():
    p = make()
    ring = item("ring-1")
    p.equip("finger-left", ring)
    p.equip("finger-right", ring)
    assert p.counts() == {"dragon": 1}
    assert p.active_tiers() == {}
    assert p.stats() == {}


def test_two_items_with_same_name_are_distinct_pieces():
    p = make()
    p.equip("head", item("helm-a", name="Dragon Helm"))
    p.equip("chest", item("helm-b", name="Dragon Helm"))
    assert p.counts() == {"dragon": 2}
    assert p.active_tiers() == {"dragon": 2}
    assert p.stats() == {"strength": 10}


def test_stored_items_never_count_toward_set_pieces():
    p = make()
    p.equip("head", item("helm-a"))
    p.equip("chest", item("helm-b"))
    p.store(item("bank-helm", name="Dragon Helm"))
    p.store(item("bank-boots", name="Dragon Boots"))
    assert p.counts() == {"dragon": 2}
    assert p.active_tiers() == {"dragon": 2}


def test_locked_item_is_excluded_from_count_and_bonus():
    p = make()
    equip_four(p)
    p.lock("i1", True)
    assert p.counts() == {"dragon": 3}
    assert p.active_tiers() == {"dragon": 2}
    assert p.stats() == {"strength": 10}
    p.lock("i1", False)
    assert p.counts() == {"dragon": 4}


def test_unequip_replaces_the_whole_bonus_batch():
    p = make()
    equip_four(p)
    assert p.stats() == {"strength": 30}
    p.unequip("slot-3")
    assert p.stats() == {"strength": 10}
    p.unequip("slot-2")
    p.unequip("slot-1")
    assert p.stats() == {}
    assert p.active_tiers() == {}


def test_only_highest_satisfied_tier_applies():
    p = make()
    equip_four(p)
    assert p.active_tiers() == {"dragon": 4}
    assert p.stats() == {"strength": 30}


def test_two_sets_each_contribute_their_own_tier():
    p = make()
    p.define_set("frost", {2: {"intellect": 7}, 4: {"intellect": 20}})
    for index in range(3):
        p.equip("dragon-%d" % index, item("d%d" % index, set_id="dragon"))
        p.equip("frost-%d" % index, item("f%d" % index, set_id="frost"))
    assert p.counts() == {"dragon": 3, "frost": 3}
    assert p.active_tiers() == {"dragon": 2, "frost": 2}
    assert p.stats() == {"strength": 10, "intellect": 7}


def test_percent_bonuses_multiply_between_sets():
    p = make()
    p.define_set("dragon", {2: {"damage_pct": 10}})
    p.define_set("frost", {2: {"damage_pct": 20}})
    for index in range(2):
        p.equip("dragon-%d" % index, item("d%d" % index, set_id="dragon"))
        p.equip("frost-%d" % index, item("f%d" % index, set_id="frost"))
    assert p.stats()["damage_pct"] == pytest.approx(32.0)


def test_pvp_disables_flagged_set_bonus():
    p = sba.SetBonusAggregator()
    p.define_set("dragon", DRAGON, pvp_allowed=False)
    equip_four(p)
    assert p.stats("pve") == {"strength": 30}
    assert p.stats("pvp") == {}
    assert p.active_tiers("pvp") == {}


def test_preview_never_changes_combat_stats():
    p = make()
    equip_four(p)
    before = p.stats()
    loadout = [{"slot": "slot-0", "item": item("x0", set_id="frost")}]
    p.preview(loadout)
    assert p.stats() == before
    assert p.counts() == {"dragon": 4}


def test_preview_uses_its_own_loadout_snapshot():
    p = make()
    p.define_set("frost", {2: {"intellect": 33}})
    p.equip("slot-0", item("dragon-0"))
    preview = p.preview([
        {"slot": "slot-0", "item": item("f0", set_id="frost")},
        {"slot": "slot-1", "item": item("f1", set_id="frost")},
    ])
    assert preview["stats"] == {"intellect": 33}
    assert preview["tiers"] == {"frost": 2}
    assert p.stats() == {}
    assert p.equipment()["slot-0"]["item_id"] == "dragon-0"


def test_redefining_a_set_refreshes_stats_without_stale_bonus():
    p = sba.SetBonusAggregator()
    p.define_set("dragon", {2: {"strength": 5}})
    p.equip("head", item("h1"))
    p.equip("chest", item("c1"))
    assert p.stats() == {"strength": 5}
    p.define_set("dragon", {2: {"strength": 99, "stamina": 4}})
    assert p.stats() == {"strength": 99, "stamina": 4}


def test_item_without_set_contributes_nothing():
    p = make()
    p.equip("head", item("plain-helm", set_id=None))
    p.equip("chest", item("plain-chest", set_id=None))
    assert p.counts() == {}
    assert p.stats() == {}


def test_unknown_set_id_contributes_nothing():
    p = make()
    p.equip("head", item("mystery-1", set_id="undefined"))
    p.equip("chest", item("mystery-2", set_id="undefined"))
    assert p.stats() == {}
    assert p.active_tiers() == {}


def test_cross_server_set_alias_maps_to_canonical_set():
    p = sba.SetBonusAggregator()
    p.define_set("dragon", {2: {"strength": 12}}, aliases=["s2:dragon"])
    p.equip("head", item("h1", set_id="s2:dragon"))
    p.equip("chest", item("c1", set_id="s2:dragon"))
    assert p.counts() == {"dragon": 2}
    assert p.active_tiers() == {"dragon": 2}
    assert p.stats() == {"strength": 12}
