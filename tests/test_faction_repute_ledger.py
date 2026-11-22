import pytest

import faction_repute_ledger as frl


def make():
    p = frl.FactionReputeLedger()
    p.define_faction("A", minimum=0, maximum=1000, hostile_below=100,
                     vendors={400: ["tabard"], 800: ["mount"]})
    p.define_faction("B", minimum=0, maximum=1000, hostile_below=100,
                     vendors={400: ["banner"]})
    return p


def test_duplicate_source_grants_reputation_only_once():
    p = make()
    first = p.grant("char-1", "A", "quest:rescue", 100, now=0)
    assert first["amount"] == 100
    again = p.grant("char-1", "A", "quest:rescue", 100, now=1)
    assert again["status"] == "already_granted"
    assert again["amount"] == 0
    assert p.reputation("char-1", "A") == 100


def test_event_and_guild_bonus_stack_additively():
    p = make()
    out = p.grant("char-1", "A", "event:double", 100, now=0,
                  multiplier=2.0, bonus_pct=50)
    assert out["amount"] == 250
    assert p.reputation("char-1", "A") == 250


def test_upper_bound_clamps_and_records_discarded_amount():
    p = make()
    p.grant("char-1", "A", "quest:a", 950, now=0)
    out = p.grant("char-1", "A", "quest:b", 100, now=1)
    assert out["amount"] == 50
    assert out["discarded"] == 50
    assert p.reputation("char-1", "A") == 1000


def test_lower_bound_clamps_at_faction_minimum():
    p = make()
    p.grant("char-1", "A", "quest:a", 10, now=0)
    out = p.adjust("char-1", "A", -100, "gm-1", "exploit", now=1)
    assert out["reputation"] == 0
    assert p.reputation("char-1", "A") == 0


def test_switch_zeroes_only_the_opposing_faction():
    p = make()
    p.grant("char-1", "A", "quest:a", 500, now=0)
    p.grant("char-1", "B", "quest:b", 200, now=1)
    out = p.switch_faction("char-1", "A", "B", now=2)
    assert out["status"] == "switched"
    assert p.reputation("char-1", "A") == 0
    assert p.reputation("char-1", "B") == 200


def test_switch_refreshes_vendor_permissions():
    p = make()
    p.grant("char-1", "A", "quest:a", 500, now=0)
    assert p.vendors("char-1", "A") == ["tabard"]
    p.switch_faction("char-1", "A", "B", now=2)
    assert p.vendors("char-1", "A") == []
    assert p.vendors("char-1", "B") == []


def test_characters_keep_independent_reputation():
    p = make()
    p.grant("char-1", "A", "quest:a", 300, now=0)
    assert p.reputation("char-2", "A") == 0
    assert p.reputation("char-1", "A") == 300


def test_standing_becomes_hostile_below_threshold():
    p = make()
    p.grant("char-1", "A", "quest:a", 150, now=0)
    assert p.standing("char-1", "A") == "neutral"
    p.adjust("char-1", "A", -100, "gm-1", "betrayal", now=1)
    assert p.standing("char-1", "A") == "hostile"


def test_dropping_below_threshold_revokes_vendor_unlock():
    p = make()
    p.grant("char-1", "A", "quest:a", 500, now=0)
    assert p.vendors("char-1", "A") == ["tabard"]
    p.adjust("char-1", "A", -200, "gm-1", "rollback", now=1)
    assert p.vendors("char-1", "A") == []


def test_rollback_marks_purchased_items_as_revoked():
    p = make()
    p.grant("char-1", "A", "quest:a", 500, now=0)
    p.purchase("char-1", "A", "tabard", now=1)
    assert p.purchases("char-1")[0]["revoked"] is False
    p.adjust("char-1", "A", -200, "gm-1", "exploit", now=2)
    assert p.purchases("char-1")[0]["revoked"] is True


def test_weekly_decay_is_applied_once_per_week():
    p = make()
    p.grant("char-1", "A", "quest:a", 300, now=0)
    first = p.weekly_decay("char-1", "A", "2026w10", amount=50, now=1)
    assert first["status"] == "applied"
    second = p.weekly_decay("char-1", "A", "2026w10", amount=50, now=2)
    assert second["status"] == "already_applied"
    assert p.reputation("char-1", "A") == 250
    p.weekly_decay("char-1", "A", "2026w11", amount=50, now=3)
    assert p.reputation("char-1", "A") == 200


def test_read_after_grant_is_never_stale():
    p = make()
    p.grant("char-1", "A", "quest:a", 400, now=0)
    assert p.reputation("char-1", "A") == 400
    p.grant("char-1", "A", "quest:b", 100, now=1)
    assert p.reputation("char-1", "A") == 500


def test_cross_server_result_is_booked_to_named_faction_only():
    p = make()
    p.grant("char-1", "B", "battle:cross-server", 120, now=0)
    assert p.reputation("char-1", "B") == 120
    assert p.reputation("char-1", "A") == 0


def test_ledger_records_every_change_with_actor():
    p = make()
    p.grant("char-1", "A", "quest:a", 300, now=0)
    p.adjust("char-1", "A", -50, "gm-7", "reported", now=5)
    entries = p.ledger("char-1", "A")
    assert [e["kind"] for e in entries] == ["grant", "adjust"]
    assert entries[1]["actor"] == "gm-7"
    assert entries[1]["reason"] == "reported"
    assert entries[1]["amount"] == -50


def test_restore_keeps_ledger_and_grant_idempotency():
    p = make()
    p.grant("char-1", "A", "quest:a", 300, now=0)
    state = p.snapshot()
    q = make()
    q.restore(state)
    assert q.reputation("char-1", "A") == 300
    assert q.grant("char-1", "A", "quest:a", 300, now=1)["status"] == "already_granted"
    assert len(q.ledger("char-1", "A")) == 1
