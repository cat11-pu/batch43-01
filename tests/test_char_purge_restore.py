import pytest

import char_purge_restore as cpr


def make(**kwargs):
    return cpr.CharPurgeRestore(**kwargs)


def hero(p, cid="char-1", account="acc-1", nickname="Alice"):
    p.create_character(cid, account, nickname, guild_rank="officer",
                       permissions=["bank", "guild-invite"],
                       inventory={"sword": 1},
                       mail=[{"item": "epic-chest", "id": "m1"}],
                       achievements={"explorer": 5}, now=0)
    p.fund_account(account, 1000)
    return cid


def test_purged_character_items_cannot_be_taken_by_siblings():
    p = make()
    hero(p)
    p.create_character("char-2", "acc-1", "Bob", now=0)
    p.purge("char-1", "gm-1", now=10)
    with pytest.raises(cpr.PurgeError) as exc:
        p.transfer_item("char-1", "char-2", "sword", 1)
    assert exc.value.code == "character_purged"
    assert p.inventory("char-2") == {}


def test_purge_failure_rolls_back_every_step():
    p = make()
    hero(p)
    with pytest.raises(cpr.PurgeError) as exc:
        p.purge("char-1", "gm-1", now=10, fail_step="delist_auction")
    assert exc.value.code == "purge_failed"
    assert p.inventory("char-1") == {"sword": 1}
    assert p.login("char-1", now=11)["status"] == "ok"
    assert [row["character_id"] for row in p.leaderboard()] == ["char-1"]


def test_purged_character_leaves_leaderboard_and_search():
    p = make()
    hero(p)
    assert p.search("Alice")
    p.purge("char-1", "gm-1", now=10)
    assert p.leaderboard() == []
    assert p.search("Alice") == []


def test_purge_snapshot_returns_mail_after_restore():
    p = make()
    hero(p)
    p.purge("char-1", "gm-1", now=10)
    p.sweep(now=10)
    out = p.restore("char-1", "acc-1", now=10 ** 6)
    assert out["status"] == "restored"
    mail = p.mailbox("char-1")
    assert len(mail) == 1
    assert mail[0]["item"] == "epic-chest"


def test_nickname_is_reserved_while_character_is_deleted():
    p = make(reservation=5000)
    hero(p)
    p.purge("char-1", "gm-1", now=10)
    with pytest.raises(cpr.PurgeError) as exc:
        p.claim_nickname("acc-9", "Alice", now=100)
    assert exc.value.code == "name_reserved"
    out = p.restore("char-1", "acc-1", now=200)
    assert out["nickname"] == "Alice"


def test_duplicate_restore_is_idempotent_and_charges_fee_once():
    p = make(fee=100)
    hero(p)
    p.purge("char-1", "gm-1", now=10)
    p.restore("char-1", "acc-1", now=20)
    assert p.account_funds("acc-1") == 900
    again = p.restore("char-1", "acc-1", now=21)
    assert again["status"] == "already_restored"
    assert p.account_funds("acc-1") == 900


def test_restore_limit_rejects_without_deleting_older_character():
    p = make(fee=100, max_restored=1)
    hero(p, "char-1", "acc-1", "Alice")
    p.create_character("char-2", "acc-1", "Bob", now=0)
    p.purge("char-1", "gm-1", now=10)
    p.restore("char-1", "acc-1", now=20)
    p.purge("char-2", "gm-1", now=30)
    with pytest.raises(cpr.PurgeError) as exc:
        p.restore("char-2", "acc-1", now=40)
    assert exc.value.code == "restore_limit"
    assert p.login("char-1", now=41)["status"] == "ok"


def test_restore_by_another_account_is_rejected():
    p = make()
    hero(p)
    p.purge("char-1", "gm-1", now=10)
    with pytest.raises(cpr.PurgeError) as exc:
        p.restore("char-1", "acc-2", now=20)
    assert exc.value.code == "not_owner"


def test_restore_returns_guild_rank_and_permissions():
    p = make()
    hero(p)
    p.purge("char-1", "gm-1", now=10)
    out = p.restore("char-1", "acc-1", now=20)
    assert out["guild_rank"] == "officer"
    assert sorted(out["permissions"]) == ["bank", "guild-invite"]


def test_account_achievements_survive_purge_and_restore():
    p = make()
    hero(p)
    before = p.account_achievements("acc-1")
    p.purge("char-1", "gm-1", now=10)
    p.sweep(now=10)
    assert p.account_achievements("acc-1") == before
    p.restore("char-1", "acc-1", now=20)
    assert p.account_achievements("acc-1") == before


def test_season_rewards_exclude_purged_characters():
    p = make()
    hero(p, "char-1", "acc-1", "Alice")
    p.create_character("char-2", "acc-2", "Bob", now=0)
    p.purge("char-1", "gm-1", now=10)
    paid = p.season_rewards(50, now=20)
    assert paid == ["char-2"]


def test_crashed_purge_is_finished_by_sweep():
    p = make()
    hero(p)
    out = p.purge("char-1", "gm-1", now=10, crash_after="delist_auction")
    assert out["status"] == "pending"
    with pytest.raises(cpr.PurgeError) as exc:
        p.login("char-1", now=11)
    assert exc.value.code == "character_purging"
    done = p.sweep(now=20)
    assert done["completed"] == ["char-1"]
    with pytest.raises(cpr.PurgeError) as exc:
        p.login("char-1", now=21)
    assert exc.value.code == "character_purged"
    assert p.inventory("char-1") == {}
    assert p.leaderboard() == []


def test_login_is_blocked_immediately_after_purge():
    p = make()
    hero(p)
    p.purge("char-1", "gm-1", now=10)
    with pytest.raises(cpr.PurgeError) as exc:
        p.login("char-1", now=11)
    assert exc.value.code == "character_purged"


def test_sweep_without_pending_tasks_is_a_noop():
    p = make()
    hero(p)
    out = p.sweep(now=10)
    assert out["completed"] == []
    assert p.inventory("char-1") == {"sword": 1}


def test_restored_character_can_login_and_returns_to_leaderboard():
    p = make()
    hero(p)
    p.purge("char-1", "gm-1", now=10)
    p.restore("char-1", "acc-1", now=20)
    assert p.login("char-1", now=21)["status"] == "ok"
    assert [row["character_id"] for row in p.leaderboard()] == ["char-1"]
    assert p.search("Alice")[0]["character_id"] == "char-1"
