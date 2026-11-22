import pytest

import battlepass_tier_ledger as btl


TIERS = {
    1: {"free": {"gold": 10}, "premium": {"skin": "wings"}},
    2: {"free": {"gold": 20}, "premium": {"skin": "helm"}},
    3: {"free": {"gold": 30}, "premium": {"skin": "cape"}},
}


def make(**kwargs):
    kwargs.setdefault("tiers", TIERS)
    return btl.BattlepassTierLedger(**kwargs)


def test_duplicate_source_grants_experience_once():
    p = make()
    first = p.grant("acc-1", 100, "quest:kill-10", now=0)
    assert first["granted"] == 100
    again = p.grant("acc-1", 100, "quest:kill-10", now=1)
    assert again["status"] == "already_granted"
    assert again["granted"] == 0
    assert p.progress("acc-1")["exp"] == 100


def test_second_redeem_of_same_tier_returns_tier_redeemed():
    p = make()
    p.grant("acc-1", 100, "quest:a", now=0)
    first = p.redeem("acc-1", 1, "free", now=1)
    assert first["status"] == "redeemed"
    with pytest.raises(btl.PassError) as exc:
        p.redeem("acc-1", 1, "free", now=2)
    assert exc.value.code == "tier_redeemed"
    assert p.progress("acc-1")["redeemed"] == ["free:1"]


def test_reclaim_uses_the_same_idempotency_records_as_redeem():
    p = make()
    p.grant("acc-1", 200, "quest:a", now=0)
    p.redeem("acc-1", 1, "free", now=1)
    with pytest.raises(btl.PassError) as exc:
        p.reclaim("acc-1", 1, "free", now=2)
    assert exc.value.code == "tier_redeemed"


def test_premium_track_requires_entitlement():
    p = make()
    p.grant("acc-1", 100, "quest:a", now=0)
    with pytest.raises(btl.PassError) as exc:
        p.redeem("acc-1", 1, "premium", now=1)
    assert exc.value.code == "premium_locked"
    p.purchase("acc-1", "order-9", now=2, platform="console")
    assert p.entitlement("acc-1")["premium"] is True
    assert p.redeem("acc-1", 1, "premium", now=3)["status"] == "redeemed"


def test_free_track_is_redeemable_without_purchase():
    p = make()
    p.grant("acc-1", 100, "quest:a", now=0)
    assert p.redeem("acc-1", 1, "free", now=1)["status"] == "redeemed"
    assert p.entitlement("acc-1")["premium"] is False


def test_redeem_above_current_level_is_locked():
    p = make()
    p.grant("acc-1", 100, "quest:a", now=0)
    with pytest.raises(btl.PassError) as exc:
        p.redeem("acc-1", 2, "free", now=1)
    assert exc.value.code == "tier_locked"
    assert p.progress("acc-1")["redeemed"] == []


def test_refund_without_claimed_rewards_revokes_entitlement():
    p = make()
    p.purchase("acc-1", "order-1", now=0)
    out = p.refund("acc-1", "order-1", now=5)
    assert out["status"] == "refunded"
    assert p.entitlement("acc-1")["premium"] is False
    p.grant("acc-1", 100, "quest:a", now=6)
    with pytest.raises(btl.PassError) as exc:
        p.redeem("acc-1", 1, "premium", now=7)
    assert exc.value.code == "premium_locked"


def test_refund_after_claiming_creates_debt_and_blocks_premium():
    p = make()
    p.purchase("acc-1", "order-1", now=0)
    p.grant("acc-1", 100, "quest:a", now=1)
    p.redeem("acc-1", 1, "premium", now=2)
    out = p.refund("acc-1", "order-1", now=3)
    assert out["debt"] == 1
    p.purchase("acc-1", "order-2", now=4)
    p.grant("acc-1", 100, "quest:b", now=5)
    with pytest.raises(btl.PassError) as exc:
        p.redeem("acc-1", 2, "premium", now=6)
    assert exc.value.code == "reward_debt"


def test_season_switch_keeps_old_experience_out_of_new_season():
    p = make()
    p.grant("acc-1", 50, "quest:old", now=900)
    out = p.season_switch("s2", freeze_at=1000, now=1000)
    assert out["season"] == "s2"
    p.grant("acc-1", 100, "quest:new", now=1001)
    assert p.progress("acc-1")["season"] == "s2"
    assert p.progress("acc-1")["exp"] == 100
    assert p.progress("acc-1", season="s1")["exp"] == 50


def test_pre_freeze_grant_after_switch_is_rejected():
    p = make()
    p.season_switch("s2", freeze_at=1000, now=1000)
    with pytest.raises(btl.PassError) as exc:
        p.grant("acc-1", 10, "quest:late", now=999)
    assert exc.value.code == "season_closed"
    p.grant("acc-1", 10, "quest:ok", now=1001)
    assert p.progress("acc-1")["exp"] == 10


def test_penalty_lowers_level_and_keeps_redeemed_tiers_closed():
    p = make()
    p.grant("acc-1", 300, "quest:a", now=0)
    p.redeem("acc-1", 3, "free", now=1)
    out = p.penalty("acc-1", 2, "gm-1", "exploit", now=2)
    assert out["level"] == 1
    with pytest.raises(btl.PassError) as exc:
        p.redeem("acc-1", 2, "free", now=3)
    assert exc.value.code == "tier_locked"
    with pytest.raises(btl.PassError) as exc:
        p.redeem("acc-1", 3, "free", now=4)
    assert exc.value.code == "tier_redeemed"


def test_server_transfer_keeps_account_progress():
    p = make()
    p.grant("acc-1", 150, "quest:a", now=0)
    p.transfer("acc-1", "s9")
    assert p.progress("acc-1")["exp"] == 150
    assert p.progress("acc-1")["server"] == "s9"


def test_manual_adjust_requires_reason_and_is_audited():
    p = make()
    with pytest.raises(btl.PassError) as exc:
        p.adjust("acc-1", 1, "gm-1", "", now=0)
    assert exc.value.code == "reason_required"
    p.adjust("acc-1", 1, "gm-1", "compensation", now=1)
    entries = [e for e in p.audit() if e["action"] == "adjust"]
    assert entries[0]["actor"] == "gm-1"
    assert entries[0]["reason"] == "compensation"
    assert entries[0]["at"] == 1


def test_free_and_premium_rewards_do_not_cross():
    p = make()
    p.purchase("acc-1", "order-1", now=0)
    p.grant("acc-1", 100, "quest:a", now=1)
    free = p.redeem("acc-1", 1, "free", now=2)
    premium = p.redeem("acc-1", 1, "premium", now=3)
    assert free["reward"] == {"gold": 10}
    assert premium["reward"] == {"skin": "wings"}
    assert set(p.progress("acc-1")["redeemed"]) == {"free:1", "premium:1"}


def test_restore_keeps_experience_and_redemption_records():
    p = make()
    p.grant("acc-1", 200, "quest:a", now=0)
    p.redeem("acc-1", 1, "free", now=1)
    state = p.snapshot()
    q = make()
    q.restore(state)
    assert q.progress("acc-1")["exp"] == 200
    with pytest.raises(btl.PassError) as exc:
        q.redeem("acc-1", 1, "free", now=2)
    assert exc.value.code == "tier_redeemed"
    assert q.redeem("acc-1", 2, "free", now=3)["status"] == "redeemed"
