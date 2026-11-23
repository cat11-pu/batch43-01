import pytest

import faction_repute_ledger as frl


def make():
    ledger = frl.FactionReputeLedger()
    ledger.define_faction("A", minimum=0, maximum=1000, hostile_below=100,
                          vendors={400: ["tabard"], 800: ["mount"]})
    ledger.define_faction("B", minimum=-500, maximum=500, hostile_below=0,
                          vendors={200: ["banner"]})
    return ledger


def test_already_granted_writes_no_second_ledger_entry():
    ledger = make()
    ledger.grant("c1", "A", "q:1", 100, now=0)
    duplicate = ledger.grant("c1", "A", "q:1", 100, now=1)
    assert duplicate["status"] == "already_granted"
    entries = ledger.ledger("c1", "A")
    assert len(entries) == 1
    assert entries[0]["kind"] == "grant"
    assert entries[0]["discarded"] == 0


def test_clamp_discards_everything_when_already_at_cap():
    ledger = make()
    ledger.grant("c1", "A", "q:1", 1000, now=0)
    out = ledger.grant("c1", "A", "q:2", 250, now=1)
    assert out["amount"] == 0
    assert out["discarded"] == 250
    assert ledger.reputation("c1", "A") == 1000
    assert ledger.ledger("c1", "A")[-1]["discarded"] == 250


def test_adjust_clamps_to_negative_minimum():
    ledger = make()
    out = ledger.adjust("c1", "B", -999, "gm-1", "exploit", now=0)
    assert out["reputation"] == -500
    assert ledger.reputation("c1", "B") == -500
    assert ledger.standing("c1", "B") == "hostile"


def test_switch_clears_opposing_reputation_and_revokes_vendors():
    ledger = make()
    ledger.grant("c1", "A", "q:1", 500, now=0)
    ledger.purchase("c1", "A", "tabard", now=1)
    ledger.grant("c1", "B", "q:2", 300, now=2)

    ledger.switch_faction("c1", "A", "B", now=3)

    assert ledger.reputation("c1", "A") == 0
    assert ledger.reputation("c1", "B") == 300
    assert ledger.vendors("c1", "A") == []
    assert ledger.vendors("c1", "B") == ["banner"]
    purchase = ledger.purchases("c1")[0]
    assert purchase["item"] == "tabard"
    assert purchase["revoked"] is True
    switch_entry = [e for e in ledger.ledger("c1", "A") if e["kind"] == "switch"]
    assert len(switch_entry) == 1


def test_unknown_faction_and_locked_vendor_raise_coded_errors():
    ledger = make()
    with pytest.raises(frl.ReputeError) as exc:
        ledger.reputation("c1", "C")
    assert exc.value.code == "unknown_faction"

    with pytest.raises(frl.ReputeError) as exc:
        ledger.purchase("c1", "A", "mount", now=0)
    assert exc.value.code == "vendor_locked"


def test_reputation_is_scoped_per_character():
    ledger = make()
    ledger.grant("c1", "A", "q:1", 300, now=0)
    ledger.grant("c2", "A", "q:1", 900, now=1)
    assert ledger.reputation("c1", "A") == 300
    assert ledger.reputation("c2", "A") == 900
    assert ledger.reputation("c3", "A") == 0
    # The same source can still be granted once to each character.
    assert ledger.grant("c2", "A", "q:1", 10, now=2)["status"] == "already_granted"


def test_ledger_filters_by_character_and_faction():
    ledger = make()
    ledger.grant("c1", "A", "q:1", 100, now=0)
    ledger.grant("c1", "B", "q:2", 100, now=1)
    ledger.grant("c2", "A", "q:3", 100, now=2)

    assert [e["character"] for e in ledger.ledger("c1")] == ["c1", "c1"]
    assert all(e["faction"] == "A" for e in ledger.ledger(faction="A"))
    only = ledger.ledger("c1", "B")
    assert len(only) == 1
    assert only[0]["source"] == "q:2"


def test_decay_is_idempotent_and_bounded():
    ledger = make()
    ledger.grant("c1", "A", "q:1", 30, now=0)
    first = ledger.weekly_decay("c1", "A", "2026w12", amount=50, now=1)
    assert first["status"] == "applied"
    assert first["reputation"] == 0
    second = ledger.weekly_decay("c1", "A", "2026w12", amount=50, now=2)
    assert second["status"] == "already_applied"
    assert second["amount"] == 0
    assert ledger.reputation("c1", "A") == 0


def test_disk_persistence_round_trip_keeps_full_history(tmp_path):
    ledger = make()
    ledger.grant("c1", "A", "q:1", 500, now=0)
    ledger.adjust("c1", "A", -100, "gm-9", "rollback", now=1)
    ledger.weekly_decay("c1", "A", "2026w12", amount=50, now=2)
    path = tmp_path / "repute.json"
    ledger.save(str(path))

    rebuilt = frl.FactionReputeLedger()
    rebuilt.load(str(path))

    assert rebuilt.reputation("c1", "A") == 350
    assert rebuilt.grant("c1", "A", "q:1", 1)["status"] == "already_granted"
    assert rebuilt.weekly_decay("c1", "A", "2026w12", amount=50)["status"] == "already_applied"
    entries = rebuilt.ledger("c1", "A")
    assert [e["kind"] for e in entries] == ["grant", "adjust", "decay"]
    assert entries[1]["actor"] == "gm-9"
    assert rebuilt.vendors("c1", "A") == []  # 350 is below the 400 threshold


def test_snapshot_restore_preserves_everything_in_memory():
    ledger = make()
    ledger.grant("c1", "A", "q:1", 500, now=0)
    ledger.purchase("c1", "A", "tabard", now=1)

    clone = frl.FactionReputeLedger()
    clone.restore(ledger.snapshot())

    assert clone.vendors("c1", "A") == ["tabard"]
    assert clone.purchases("c1")[0]["item"] == "tabard"
    assert len(clone.ledger()) == 2
    # Mutating the snapshot must not touch the live ledger.
    assert ledger.reputation("c1", "A") == 500
