import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import guild_vault_permission as gvp


def make_contended_vault():
    vault = gvp.GuildVaultPermission()
    vault.add_member("G1", "c1", rank=3, account_id="acc1")
    vault.add_member("G1", "c2", rank=3, account_id="acc2")
    vault.add_slot("G1", 0, "gem", 1)
    return vault


def test_concurrent_last_item_has_exactly_one_winner():
    vault = make_contended_vault()
    barrier = threading.Barrier(2)

    def withdraw(character_id):
        barrier.wait()
        try:
            vault.withdraw("G1", character_id, 0, 1)
        except gvp.VaultError as exc:
            return exc.code
        return "OK"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(withdraw, ("c1", "c2")))

    assert results.count("OK") == 1
    assert results.count("OUT_OF_STOCK") == 1
    assert vault.vault("G1")[0]["quantity"] == 0
    assert len(vault.log("G1")) == 1


def test_failed_stock_check_does_not_consume_quota_or_write_log():
    vault = gvp.GuildVaultPermission()
    vault.add_member("G1", "c1", rank=3, account_id="acc1")
    vault.set_daily_limit("G1", 3, 1)
    vault.add_slot("G1", 0, "ore", 0)
    before = vault.dump()

    with pytest.raises(gvp.VaultError) as exc:
        vault.withdraw("G1", "c1", 0, 1)

    assert exc.value.code == "OUT_OF_STOCK"
    assert vault.dump() == before
