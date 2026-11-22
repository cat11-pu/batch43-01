import pytest

import vendor_restock_clock as vrc


def make(**kwargs):
    return vrc.VendorRestockClock(**kwargs)


def stocked(v, vendor="v1", item="potion", stock=10, cap=10,
            interval=1000, next_at=1000, limit=None, price=5):
    return v.add_item(vendor, item, stock, cap, interval, next_at,
                      purchase_limit=limit, price=price)


def test_last_item_cannot_be_sold_twice():
    v = make()
    stocked(v, stock=1)
    first = v.buy("v1", "potion", "acc1", "c1", 1, now=0)
    assert first["bought"] == 1
    assert first["stock"] == 0
    with pytest.raises(vrc.VendorError) as exc:
        v.buy("v1", "potion", "acc2", "c2", 1, now=1)
    assert exc.value.code == "OUT_OF_STOCK"
    assert v.stock("v1", "potion") == 0


def test_restock_resets_to_cap_instead_of_adding():
    v = make()
    stocked(v, stock=3, cap=10)
    out = v.restock("v1", now=1000)
    assert out["restocked"] == ["potion"]
    assert v.stock("v1", "potion") == 10


def test_account_limit_survives_restart():
    v = make()
    stocked(v, limit=2)
    v.buy("v1", "potion", "acc1", "c1", 2, now=0)
    with pytest.raises(vrc.VendorError) as exc:
        v.buy("v1", "potion", "acc1", "c1", 1, now=1)
    assert exc.value.code == "LIMIT_EXCEEDED"
    state = v.dump()
    restored = make()
    restored.load(state)
    with pytest.raises(vrc.VendorError):
        restored.buy("v1", "potion", "acc1", "c1", 1, now=2)


def test_restock_time_is_absolute_across_restart():
    v = make()
    stocked(v, stock=4, next_at=1000)
    state = v.dump()
    restored = make()
    restored.load(state)
    assert restored.next_restock("v1", "potion") == 1000


def test_restock_never_exceeds_cap_after_sales():
    v = make()
    stocked(v, stock=10, cap=10)
    v.buy("v1", "potion", "acc1", "c1", 1, now=0)
    v.restock("v1", now=1000)
    assert v.stock("v1", "potion") == 10


def test_full_bag_purchase_is_rejected_without_charge():
    charges = []

    def wallet(amount):
        charges.append(amount)
        return True

    v = make(wallet=wallet)
    stocked(v)
    with pytest.raises(vrc.VendorError) as exc:
        v.buy("v1", "potion", "acc1", "c1", 1, now=0, bag_free_slots=0)
    assert exc.value.code == "BAG_FULL"
    assert charges == []
    assert v.stock("v1", "potion") == 10
    assert v.outbox() == []


def test_limit_is_shared_between_characters_of_one_account():
    v = make()
    stocked(v, limit=2)
    v.buy("v1", "potion", "acc1", "c1", 2, now=0)
    with pytest.raises(vrc.VendorError) as exc:
        v.buy("v1", "potion", "acc1", "c2", 1, now=1)
    assert exc.value.code == "LIMIT_EXCEEDED"
    assert v.account_purchases("v1", "potion", "acc1") == 2


def test_purchase_stays_committed_after_disconnect():
    v = make()
    stocked(v, stock=10)
    v.buy("v1", "potion", "acc1", "c1", 1, now=0)
    state = v.dump()
    restored = make()
    restored.load(state)
    assert restored.stock("v1", "potion") == 9
    assert restored.account_purchases("v1", "potion", "acc1") == 1


def test_restock_after_sale_does_not_undo_sale():
    v = make()
    stocked(v, stock=5, cap=10, next_at=1000)
    v.buy("v1", "potion", "acc1", "c1", 1, now=999)
    v.restock("v1", now=1000)
    assert v.stock("v1", "potion") == 10


def test_removed_item_cannot_be_bought():
    v = make()
    stocked(v)
    v.remove_item("v1", "potion")
    with pytest.raises(vrc.VendorError) as exc:
        v.buy("v1", "potion", "acc1", "c1", 1, now=0)
    assert exc.value.code == "ITEM_UNAVAILABLE"


def test_describe_exposes_absolute_restock_epoch():
    v = make()
    stocked(v, next_at=1700000000)
    info = v.describe("v1", "potion")
    assert info["next_restock_at"] == 1700000000
    assert info["cap"] == 10


def test_payment_failure_leaves_stock_and_limit_untouched():
    v = make(wallet=lambda amount: False)
    stocked(v, limit=2)
    with pytest.raises(vrc.VendorError) as exc:
        v.buy("v1", "potion", "acc1", "c1", 1, now=0)
    assert exc.value.code == "PAYMENT_FAILED"
    assert v.stock("v1", "potion") == 10
    assert v.account_purchases("v1", "potion", "acc1") == 0


def test_stock_never_leaves_zero_to_cap_window():
    v = make()
    stocked(v, stock=2, cap=4)
    for quantity in (2, 2, 2):
        try:
            v.buy("v1", "potion", "acc1", "c1", quantity, now=0)
        except vrc.VendorError:
            pass
        assert 0 <= v.stock("v1", "potion") <= 4


def test_restock_schedule_advances_without_drift():
    v = make()
    stocked(v, stock=1, next_at=1000, interval=1000)
    v.restock("v1", now=1000)
    assert v.next_restock("v1", "potion") == 2000
    state = v.dump()
    restored = make()
    restored.load(state)
    assert restored.next_restock("v1", "potion") == 2000


def test_restock_and_purchase_share_one_consistent_state():
    v = make()
    stocked(v, stock=1, cap=2, next_at=1000)
    v.buy("v1", "potion", "acc1", "c1", 1, now=999)
    v.restock("v1", now=1000)
    assert v.stock("v1", "potion") == 2
    assert v.account_purchases("v1", "potion", "acc1") == 1
