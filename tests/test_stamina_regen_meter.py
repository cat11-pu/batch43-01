import pytest

import stamina_regen_meter as srm


def make(**kwargs):
    return srm.StaminaRegenMeter(**kwargs)


def test_offline_regen_counts_full_intervals_and_keeps_remainder():
    m = make(regen_seconds=300)
    out = m.tick("acc", now=899)
    assert out["current"] == 2
    assert out["remainder"] == 299


def test_partial_interval_never_grants_a_point():
    m = make(regen_seconds=300)
    out = m.tick("acc", now=299)
    assert out["current"] == 0
    assert out["remainder"] == 299


def test_two_consumers_of_last_point_only_one_succeeds():
    m = make()
    m.grant("acc", 1, now=0)
    first = m.consume("acc", 1, "txn-a", now=0)
    assert first["current"] == 0
    with pytest.raises(srm.StaminaError) as exc:
        m.consume("acc", 1, "txn-b", now=0)
    assert exc.value.code == "INSUFFICIENT_STAMINA"
    assert m.snapshot("acc")["current"] == 0


def test_purchase_beyond_cap_is_rejected_without_charge():
    charges = []

    def wallet(amount):
        charges.append(amount)
        return True

    m = make(wallet=wallet)
    m.grant("acc", 95, now=0)
    with pytest.raises(srm.StaminaError) as exc:
        m.purchase("acc", 10, now=0)
    assert exc.value.code == "CAP_EXCEEDED"
    assert charges == []
    assert m.snapshot("acc")["current"] == 95


def test_cap_increase_grants_only_the_delta():
    m = make(cap=100)
    m.grant("acc", 50, now=0)
    out = m.set_cap("acc", 120, mode="fill_delta", now=0)
    assert out["current"] == 70
    assert out["cap"] == 120


def test_daily_bonus_is_granted_once_per_day():
    m = make()
    first = m.daily_bonus("acc", day=1, amount=10, now=0)
    second = m.daily_bonus("acc", day=1, amount=10, now=60)
    assert first["granted"] == 10
    assert second["granted"] == 0
    assert second["already_claimed"] is True
    assert m.snapshot("acc")["current"] == 10


def test_refund_restores_stamina_and_is_idempotent():
    m = make()
    m.grant("acc", 10, now=0)
    m.consume("acc", 4, "txn-1", now=0)
    first = m.refund("acc", "txn-1", now=1)
    second = m.refund("acc", "txn-1", now=2)
    assert first["refunded"] == 4
    assert first["current"] == 10
    assert second["refunded"] == 0
    assert second["duplicate"] is True
    assert m.snapshot("acc")["current"] == 10


def test_maintenance_pause_does_not_bank_wall_clock_time():
    m = make(regen_seconds=300)
    m.set_maintenance(True, now=100)
    m.tick("acc", now=1000)
    assert m.snapshot("acc")["current"] == 0
    m.set_maintenance(False, now=1000)
    out = m.tick("acc", now=1300)
    assert out["current"] == 1


def test_grant_overflow_is_reported_not_silently_lost():
    m = make(cap=100)
    m.grant("acc", 90, now=0)
    out = m.grant("acc", 20, now=0)
    assert out["accepted"] == 10
    assert out["overflow"] == 10
    assert out["current"] == 100


def test_stale_client_value_is_rejected():
    m = make()
    m.grant("acc", 40, now=0)
    with pytest.raises(srm.StaminaError) as exc:
        m.consume("acc", 5, "txn-1", now=0, expected=50)
    assert exc.value.code == "STALE_STATE"
    assert m.snapshot("acc")["current"] == 40


def test_potion_at_full_stamina_is_not_consumed():
    m = make(cap=100)
    m.grant("acc", 100, now=0)
    out = m.use_potion("acc", potions=3, now=0)
    assert out["used"] == 0
    assert out["potions_left"] == 3
    assert out["reason"] == "AT_CAP"


def test_restart_preserves_remainder_and_next_tick():
    m = make(regen_seconds=300)
    m.tick("acc", now=899)
    state = m.dump()
    restored = make(regen_seconds=300)
    restored.load(state)
    out = restored.tick("acc", now=1199)
    assert out["current"] == 3
    assert m.snapshot("acc")["remainder"] == 299


def test_stamina_stays_between_zero_and_cap_after_sequence():
    m = make(cap=100)
    m.grant("acc", 3, now=0)
    m.consume("acc", 3, "txn-1", now=0)
    with pytest.raises(srm.StaminaError):
        m.consume("acc", 1, "txn-2", now=0)
    m.grant("acc", 200, now=0)
    snap = m.snapshot("acc")
    assert 0 <= snap["current"] <= snap["cap"]


def test_same_consume_txn_is_charged_once():
    m = make()
    m.grant("acc", 10, now=0)
    first = m.consume("acc", 4, "txn-1", now=0)
    second = m.consume("acc", 4, "txn-1", now=0)
    assert first["current"] == 6
    assert second["current"] == 6
    assert second["duplicate"] is True


def test_clock_rewind_is_refused():
    m = make()
    m.tick("acc", now=100, )
    with pytest.raises(srm.StaminaError) as exc:
        m.tick("acc", now=50)
    assert exc.value.code == "CLOCK_REWIND"
    assert m.snapshot("acc")["current"] == 0
