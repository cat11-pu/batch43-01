import pytest

import nickname_lease_book as nlb


def make(**kwargs):
    return nlb.NicknameLeaseBook(**kwargs)


def claimed(p, account="acc-1", nickname="Alice", now=0):
    p.grant_card(account, 1)
    return p.claim(account, nickname, now=now, card=True)


def test_taken_nickname_does_not_consume_rename_card():
    p = make()
    claimed(p, "acc-1", "Alice")
    p.grant_card("acc-2", 1)
    with pytest.raises(nlb.NicknameError) as exc:
        p.claim("acc-2", "Alice", now=1, card=True)
    assert exc.value.code == "name_taken"
    assert p.cards("acc-2") == 1
    assert p.resolve("Alice") == "acc-1"


def test_only_one_concurrent_claim_wins():
    p = make()
    p.grant_card("acc-1", 1)
    p.grant_card("acc-2", 1)
    p.claim("acc-1", "Bob", now=0, card=True)
    with pytest.raises(nlb.NicknameError) as exc:
        p.claim("acc-2", "Bob", now=0, card=True)
    assert exc.value.code == "name_taken"
    assert p.resolve("Bob") == "acc-1"


def test_normalization_catches_width_and_space_variants():
    p = make()
    claimed(p, "acc-1", "Alice")
    p.grant_card("acc-2", 2)
    for variant in ("Ａｌｉｃｅ", "  alice  ", "ALICE"):
        with pytest.raises(nlb.NicknameError) as exc:
            p.claim("acc-2", variant, now=1, card=True)
        assert exc.value.code == "name_taken"
    assert p.cards("acc-2") == 2


def test_rename_releases_old_nickname_immediately():
    p = make()
    claimed(p, "acc-1", "Alice")
    p.grant_card("acc-1", 1)
    p.rename("acc-1", "Alicia", now=10, card=True)
    assert p.resolve("Alice") is None
    p.grant_card("acc-2", 1)
    out = p.claim("acc-2", "Alice", now=11, card=True)
    assert out["status"] == "claimed"
    assert p.resolve("Alice") == "acc-2"


def test_failed_rename_keeps_old_nickname_and_card():
    p = make()
    claimed(p, "acc-1", "Alice")
    claimed(p, "acc-2", "Bob")
    p.grant_card("acc-1", 1)
    with pytest.raises(nlb.NicknameError) as exc:
        p.rename("acc-1", "Bob", now=10, card=True)
    assert exc.value.code == "name_taken"
    assert p.nickname_of("acc-1") == "Alice"
    assert p.cards("acc-1") == 1


def test_successful_rename_charges_exactly_one_card():
    p = make()
    claimed(p, "acc-1", "Alice")
    p.grant_card("acc-1", 3)
    p.rename("acc-1", "Alicia", now=10, card=True)
    assert p.cards("acc-1") == 2


def test_rename_cooldown_uses_server_time_argument():
    p = make(cooldown=1000)
    claimed(p, "acc-1", "Alice")
    p.grant_card("acc-1", 5)
    p.rename("acc-1", "Alicia", now=1000, card=True)
    with pytest.raises(nlb.NicknameError) as exc:
        p.rename("acc-1", "Ally", now=1500, card=True)
    assert exc.value.code == "cooldown"
    p.rename("acc-1", "Ally", now=2000, card=True)
    assert p.nickname_of("acc-1") == "Ally"


def test_deleted_account_nickname_is_reserved_for_the_period():
    p = make(reservation=5000)
    claimed(p, "acc-1", "Alice")
    p.delete_account("acc-1", now=0)
    with pytest.raises(nlb.NicknameError) as exc:
        p.claim("acc-2", "Alice", now=100, card=False)
    assert exc.value.code == "name_reserved"


def test_reservation_expires_and_frees_the_name():
    p = make(reservation=5000)
    claimed(p, "acc-1", "Alice")
    p.delete_account("acc-1", now=0)
    out = p.claim("acc-2", "Alice", now=5000, card=False)
    assert out["status"] == "claimed"
    assert p.resolve("Alice") == "acc-2"


def test_restore_inside_reservation_returns_original_nickname():
    p = make(reservation=5000)
    claimed(p, "acc-1", "Alice")
    p.delete_account("acc-1", now=0)
    out = p.restore("acc-1", now=100)
    assert out["status"] == "restored"
    assert out["nickname"] == "Alice"
    assert p.nickname_of("acc-1") == "Alice"
    with pytest.raises(nlb.NicknameError) as exc:
        p.claim("acc-2", "Alice", now=101, card=False)
    assert exc.value.code == "name_taken"


def test_restore_after_name_taken_uses_suffixed_fallback():
    p = make(reservation=5000)
    claimed(p, "acc-1", "Alice")
    p.delete_account("acc-1", now=0)
    p.claim("acc-2", "Alice", now=5000, card=False)
    out = p.restore("acc-1", now=5001)
    assert out["status"] == "restored"
    assert out["nickname"] != "Alice"
    assert out["nickname"].startswith("Alice#")
    assert p.resolve(out["nickname"]) == "acc-1"
    assert p.resolve("Alice") == "acc-2"


def test_force_rename_releases_old_name_and_records_actor():
    p = make()
    claimed(p, "acc-1", "Alice")
    out = p.force_rename("acc-1", "Alicia", "gm-1", now=20)
    assert out["nickname"] == "Alicia"
    assert p.resolve("Alice") is None
    entry = [e for e in p.history("acc-1") if e["actor"] == "gm-1"][0]
    assert entry["old"] == "Alice"
    assert entry["new"] == "Alicia"
    assert entry["at"] == 20


def test_merge_produces_unique_loginable_names():
    p = make()
    p.seed("acc-1", "Bob")
    p.seed("acc-2", "Bob")
    p.seed("acc-3", "Bob")
    out = p.merge(["acc-1", "acc-2", "acc-3"], now=30)
    names = [out[a] for a in ("acc-1", "acc-2", "acc-3")]
    assert len(set(names)) == 3
    assert names[0] == "Bob"
    assert names[1] == "Bob#2"
    assert names[2] == "Bob#3"
    for account, name in zip(("acc-1", "acc-2", "acc-3"), names):
        assert p.resolve(name) == account


def test_history_records_rename_mapping_for_social_references():
    p = make()
    claimed(p, "acc-1", "Alice")
    p.grant_card("acc-1", 1)
    p.rename("acc-1", "Alicia", now=10, card=True)
    entries = p.history("acc-1")
    assert entries[0]["old"] == "Alice"
    assert entries[0]["new"] == "Alicia"


def test_restart_keeps_reservation_expiry_absolute():
    p = make(reservation=5000)
    claimed(p, "acc-1", "Alice")
    p.delete_account("acc-1", now=0)
    state = p.dump()
    q = make(reservation=5000)
    q.load(state)
    with pytest.raises(nlb.NicknameError) as exc:
        q.claim("acc-2", "Alice", now=4999, card=False)
    assert exc.value.code == "name_reserved"
    assert q.claim("acc-2", "Alice", now=5000, card=False)["status"] == "claimed"
