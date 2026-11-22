import pytest

import guild_vault_permission as gvp


def make(**kwargs):
    return gvp.GuildVaultPermission(**kwargs)


def setup_vault(limit=None):
    g = make()
    g.add_member("G1", "c1", rank=3, account_id="acc1")
    g.add_member("G1", "c2", rank=3, account_id="acc1")
    g.add_member("G1", "leader", rank=5, account_id="acc9")
    g.set_daily_limit("G1", 3, limit)
    g.add_slot("G1", 0, "ore", 20)
    return g


def test_locked_slot_denies_rank_three():
    g = setup_vault()
    g.add_slot("G1", 1, "relic", 2, locked=True)
    with pytest.raises(gvp.VaultError) as exc:
        g.withdraw("G1", "c1", 1, 1)
    assert exc.value.code == "LOCKED_ITEM"
    assert g.vault("G1")[1]["quantity"] == 2


def test_daily_limit_is_shared_by_account_characters():
    g = setup_vault(limit=5)
    g.withdraw("G1", "c1", 0, 5)
    with pytest.raises(gvp.VaultError) as exc:
        g.withdraw("G1", "c2", 0, 1)
    assert exc.value.code == "DAILY_LIMIT_EXCEEDED"
    assert g.vault("G1")[0]["quantity"] == 15


def test_concurrent_last_item_cannot_go_negative():
    g = setup_vault()
    g.add_slot("G1", 2, "gem", 1)
    g.withdraw("G1", "c1", 2, 1)
    with pytest.raises(gvp.VaultError) as exc:
        g.withdraw("G1", "c2", 2, 1)
    assert exc.value.code == "OUT_OF_STOCK"
    assert g.vault("G1")[2]["quantity"] == 0


def test_kick_member_reports_outstanding_withdrawals():
    g = setup_vault()
    g.withdraw("G1", "c1", 0, 2)
    out = g.kick_member("G1", "c1")
    assert out["removed"] is True
    assert len(out["outstanding"]) == 1
    assert out["outstanding"][0]["item_id"] == "ore"
    assert out["outstanding"][0]["quantity"] == 2
    with pytest.raises(gvp.VaultError):
        g.withdraw("G1", "c1", 0, 1)


def test_disband_mails_all_vault_items():
    g = setup_vault()
    out = g.disband("G1", actor="leader")
    assert out["mailed"] == 1
    assert g.vault("G1") == []
    assert g.outbox()[0]["guild_id"] == "G1"
    assert g.outbox()[0]["item_id"] == "ore"


def test_permission_demotion_applies_without_relogin():
    g = setup_vault()
    g.set_rank("G1", "c1", 1)
    with pytest.raises(gvp.VaultError) as exc:
        g.withdraw("G1", "c1", 0, 1)
    assert exc.value.code == "PERMISSION_DENIED"


def test_audit_log_records_item_quantity_and_character():
    g = setup_vault()
    g.withdraw("G1", "c1", 0, 3, now=100)
    entry = g.log("G1")[0]
    assert entry["character_id"] == "c1"
    assert entry["item_id"] == "ore"
    assert entry["quantity"] == 3
    assert entry["slot"] == 0


def test_expanded_slots_inherit_template_permissions():
    g = setup_vault()
    g.add_slot("G1", 3, "relic", 1, locked=True, min_rank=4)
    g.expand("G1", 2, template_slot=3)
    slots = g.vault("G1")
    new_slots = [s for s in slots if s["slot"] in (4, 5)]
    assert len(new_slots) == 2
    with pytest.raises(gvp.VaultError) as exc:
        g.withdraw("G1", "c1", 4, 1)
    assert exc.value.code in ("LOCKED_ITEM", "PERMISSION_DENIED")


def test_bound_item_stays_bound_after_withdraw():
    g = setup_vault()
    g.add_slot("G1", 9, "bound-ore", 0, bound=True)
    g.deposit("G1", "leader", 9, "bound-ore", 1, bound=True)
    out = g.withdraw("G1", "c1", 9, 1)
    assert out["bound"] is True


def test_leader_transfer_revokes_old_leader_actions():
    g = setup_vault()
    g.transfer_leader("G1", "leader2")
    with pytest.raises(gvp.VaultError) as exc:
        g.disband("G1", actor="leader")
    assert exc.value.code in ("PERMISSION_DENIED", "NOT_LEADER")
    assert g.disband("G1", actor="leader2")["mailed"] == 1


def test_stacked_material_deposit_and_partial_withdraw():
    g = setup_vault()
    g.add_slot("G1", 6, "herb", 0)
    g.deposit("G1", "c1", 6, "herb", 3)
    g.deposit("G1", "c1", 6, "herb", 5)
    assert g.vault("G1")[6]["quantity"] == 8
    g.withdraw("G1", "c2", 6, 4)
    assert g.vault("G1")[6]["quantity"] == 4


def test_stock_never_negative_after_repeated_withdrawals():
    g = setup_vault()
    g.add_slot("G1", 7, "dust", 2)
    for quantity in (2, 1, 1):
        try:
            g.withdraw("G1", "c1", 7, quantity)
        except gvp.VaultError:
            pass
        assert g.vault("G1")[7]["quantity"] >= 0


def test_failed_withdraw_leaves_quota_and_log_untouched():
    g = setup_vault(limit=1)
    with pytest.raises(gvp.VaultError):
        g.withdraw("G1", "c1", 0, 5)
    assert g.log("G1") == []
    g.withdraw("G1", "c1", 0, 1)
    assert len(g.log("G1")) == 1


def test_restart_restores_limits_stock_and_log():
    g = setup_vault(limit=5)
    g.withdraw("G1", "c1", 0, 1, now=10)
    state = g.dump()
    restored = make()
    restored.load(state)
    assert restored.vault("G1")[0]["quantity"] == 19
    assert len(restored.log("G1")) == 1
    with pytest.raises(gvp.VaultError):
        restored.withdraw("G1", "c2", 0, 5)


def test_disband_is_idempotent_and_mails_once():
    g = setup_vault()
    first = g.disband("G1", actor="leader")
    second = g.disband("G1", actor="leader")
    assert first["mailed"] == 1
    assert second["mailed"] == 0
    assert len(g.outbox()) == 1
