import pytest

import whisper_offline_postbox as wop


def make(**kwargs):
    return wop.WhisperOfflinePostbox(**kwargs)


def sent(box, text="hello", sender="A:1", recipient="B:2", now=0,
         ttl=None, system=False):
    return box.send(sender, recipient, text, now=now, ttl=ttl,
                    system=system)


def test_expired_message_is_not_delivered():
    box = make(default_ttl=10)
    sent(box, now=0)
    assert box.deliver("B:2", now=11) == []


def test_redelivery_returns_same_id_until_ack():
    box = make()
    first = sent(box, now=0)
    one = box.deliver("B:2", now=1)
    two = box.deliver("B:2", now=2)
    assert [m["id"] for m in one] == [first["message_id"]]
    assert [m["id"] for m in two] == [first["message_id"]]
    box.ack("B:2", first["message_id"])
    assert box.deliver("B:2", now=3) == []


def test_delivery_order_is_enqueue_order_even_with_clock_skew():
    box = make()
    sent(box, text="first", now=100)
    sent(box, text="second", now=50)
    messages = box.deliver("B:2", now=101)
    assert [m["text"] for m in messages] == ["first", "second"]


def test_blocked_sender_cannot_queue_and_unblock_replays_nothing():
    box = make()
    box.block("B:2", "A:1")
    with pytest.raises(wop.PostboxError) as exc:
        sent(box, now=0)
    assert exc.value.code == "BLOCKED"
    box.unblock("B:2", "A:1")
    assert box.queue("B:2") == []
    assert box.deliver("B:2", now=1) == []


def test_filter_update_applies_to_already_queued_messages():
    box = make()
    sent(box, text="good news", now=0)
    sent(box, text="this is bad", now=1)
    box.set_filter(["bad"])
    messages = box.deliver("B:2", now=2)
    assert [m["text"] for m in messages] == ["good news"]


def test_deleted_sender_message_is_marked_unrepliable():
    box = make()
    sent(box, now=0)
    box.delete_character("A:1")
    message = box.deliver("B:2", now=1)[0]
    assert message["reply_allowed"] is False


def test_full_queue_raises_instead_of_dropping_oldest():
    box = make(queue_limit=2)
    sent(box, text="one", now=0)
    sent(box, text="two", now=1)
    with pytest.raises(wop.PostboxError) as exc:
        sent(box, text="three", now=2)
    assert exc.value.code == "QUEUE_FULL"
    assert [m["text"] for m in box.queue("B:2")] == ["one", "two"]


def test_identical_numeric_ids_in_different_regions_stay_separate():
    box = make()
    box.send("A:1", "B:7", "for b-seven", now=0)
    assert box.deliver("A:7", now=1) == []
    assert len(box.deliver("B:7", now=1)) == 1


def test_system_messages_are_delivered_first():
    box = make()
    sent(box, text="player", now=0)
    sent(box, text="maintenance", now=1, system=True)
    messages = box.deliver("B:2", now=2)
    assert messages[0]["text"] == "maintenance"
    assert messages[0]["system"] is True


def test_pushed_message_is_not_delivered_again_offline():
    box = make()
    first = sent(box, now=0)
    box.mark_pushed("B:2", first["message_id"])
    assert box.deliver("B:2", now=1) == []


def test_delete_session_clears_both_directions():
    box = make()
    box.send("A:1", "B:2", "from a", now=0)
    box.send("B:2", "A:1", "from b", now=1)
    box.delete_session("A:1", "B:2")
    assert box.queue("A:1") == []
    assert box.queue("B:2") == []


def test_duplicate_ack_is_a_no_op():
    box = make()
    first = sent(box, now=0)
    box.deliver("B:2", now=1)
    one = box.ack("B:2", first["message_id"])
    two = box.ack("B:2", first["message_id"])
    assert one["acked"] is True
    assert two["acked"] is False
    assert two["duplicate"] is True


def test_conversation_order_never_inverts():
    box = make()
    sent(box, text="a", now=0)
    sent(box, text="b", now=1)
    sent(box, text="c", now=2)
    assert [m["text"] for m in box.deliver("B:2", now=3)] == ["a", "b", "c"]


def test_one_ack_removes_the_message_once():
    box = make()
    first = sent(box, now=0)
    box.deliver("B:2", now=1)
    box.ack("B:2", first["message_id"])
    assert box.queue("B:2") == []
    second = box.ack("B:2", first["message_id"])
    assert second["acked"] is False


def test_restart_keeps_fifo_order_and_expiry():
    box = make(default_ttl=100)
    sent(box, text="one", now=0)
    sent(box, text="two", now=10)
    state = box.dump()
    restored = make(default_ttl=100)
    restored.load(state)
    assert [m["text"] for m in restored.deliver("B:2", now=50)] == ["one",
                                                                   "two"]
    assert restored.deliver("B:2", now=111) == []
