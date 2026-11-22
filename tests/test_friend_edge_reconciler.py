import pytest

import friend_edge_reconciler as fer


def make(**kwargs):
    return fer.FriendEdgeReconciler(**kwargs)


def friends_of(p, player):
    return [row["player"] for row in p.friends(player)]


def test_accept_creates_a_mutual_friendship():
    p = make()
    p.request("A", "B", now=0)
    out = p.accept("A", "B", now=1)
    assert out["status"] == "accepted"
    assert friends_of(p, "A") == ["B"]
    assert friends_of(p, "B") == ["A"]


def test_cross_request_is_rejected_as_pending():
    p = make()
    p.request("A", "B", now=0)
    with pytest.raises(fer.FriendError) as exc:
        p.request("B", "A", now=1)
    assert exc.value.code == "request_pending"
    assert len(p.pending("B")) == 1
    assert len(p.pending("A")) == 0


def test_accept_without_matching_request_is_rejected():
    p = make()
    with pytest.raises(fer.FriendError) as exc:
        p.accept("A", "B", now=0)
    assert exc.value.code == "unknown_request"
    p.request("A", "B", now=1)
    with pytest.raises(fer.FriendError) as exc:
        p.accept("B", "A", now=2)
    assert exc.value.code == "unknown_request"
    assert friends_of(p, "A") == []


def test_unilateral_remove_clears_both_sides():
    p = make()
    p.request("A", "B", now=0)
    p.accept("A", "B", now=1)
    p.remove("A", "B", now=2)
    assert friends_of(p, "A") == []
    assert friends_of(p, "B") == []


def test_block_hides_friendship_and_clears_both_lists():
    p = make()
    p.request("A", "B", now=0)
    p.accept("A", "B", now=1)
    p.block("A", "B", now=2)
    assert friends_of(p, "A") == []
    assert friends_of(p, "B") == []
    assert [row["player"] for row in p.blocked("A")] == ["B"]


def test_blocked_player_cannot_send_requests():
    p = make()
    p.block("A", "B", now=0)
    with pytest.raises(fer.FriendError) as exc:
        p.request("B", "A", now=1)
    assert exc.value.code == "blocked"
    with pytest.raises(fer.FriendError) as exc:
        p.request("A", "B", now=2)
    assert exc.value.code == "blocked"


def test_friend_limit_uses_bidirectional_unique_count():
    p = make(max_friends=2)
    for other in ("B", "C"):
        p.request(other, "A", now=0)
        p.accept(other, "A", now=1)
    assert len(friends_of(p, "A")) == 2
    p.request("D", "A", now=2)
    with pytest.raises(fer.FriendError) as exc:
        p.accept("D", "A", now=3)
    assert exc.value.code == "friend_limit"
    assert len(friends_of(p, "A")) == 2


def test_merge_dedupes_the_same_pair_without_double_counting():
    p = make(max_friends=2)
    p.merge([("A", "B"), ("A", "B"), ("A", "C")], now=0)
    assert len(friends_of(p, "A")) == 2
    assert len(set(friends_of(p, "A"))) == 2
    assert friends_of(p, "B") == ["A"]
    p.merge([("A", "B"), ("A", "C")], now=1)
    assert len(friends_of(p, "A")) == 2


def test_expired_request_cannot_be_accepted():
    p = make(request_ttl=3000)
    p.request("A", "B", now=0)
    with pytest.raises(fer.FriendError) as exc:
        p.accept("A", "B", now=3001)
    assert exc.value.code == "request_expired"
    assert friends_of(p, "A") == []


def test_offline_request_is_visible_when_recipient_returns():
    p = make()
    p.request("A", "B", now=10)
    incoming = p.pending("B")
    assert [row["player"] for row in incoming] == ["A"]
    assert p.accept("A", "B", now=20)["status"] == "accepted"
    assert friends_of(p, "B") == ["A"]


def test_rename_is_reflected_in_friend_list():
    p = make()
    p.request("A", "B", now=0)
    p.accept("A", "B", now=1)
    p.rename("B", "Bobby")
    rows = p.friends("A")
    assert rows[0]["player"] == "B"
    assert rows[0]["name"] == "Bobby"


def test_deleted_character_loses_relations_and_recreated_one_starts_clean():
    p = make()
    p.request("A", "B", now=0)
    p.accept("A", "B", now=1)
    p.delete_character("B")
    assert friends_of(p, "A") == []
    assert p.pending("A") == []
    p.request("A", "B", now=2)
    assert friends_of(p, "A") == []
    assert p.accept("A", "B", now=3)["status"] == "accepted"


def test_pending_and_friendship_survive_restart():
    p = make()
    p.request("A", "B", now=0)
    p.request("C", "D", now=1)
    p.accept("C", "D", now=2)
    state = p.dump()
    q = make()
    q.load(state)
    assert [row["player"] for row in q.pending("B")] == ["A"]
    assert friends_of(q, "C") == ["D"]
    assert q.accept("A", "B", now=3)["status"] == "accepted"


def test_request_between_existing_friends_is_rejected():
    p = make()
    p.request("A", "B", now=0)
    p.accept("A", "B", now=1)
    with pytest.raises(fer.FriendError) as exc:
        p.request("A", "B", now=2)
    assert exc.value.code == "already_friends"


def test_decline_clears_pending_without_friendship():
    p = make()
    p.request("A", "B", now=0)
    out = p.decline("A", "B", now=1)
    assert out["status"] == "declined"
    assert p.pending("B") == []
    assert friends_of(p, "B") == []
    with pytest.raises(fer.FriendError) as exc:
        p.accept("A", "B", now=2)
    assert exc.value.code == "unknown_request"
