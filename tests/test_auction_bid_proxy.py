import pytest

import auction_bid_proxy as abp


def make(**kwargs):
    return abp.AuctionBidProxy(**kwargs)


def listed(p, aid="a1", seller="seller-1", item="epic-sword",
           reserve=100, duration=60, now=0, deposit=0,
           buyout_price=800, region="s1"):
    return p.list_item(aid, seller, item, reserve, duration, now=now,
                       deposit=deposit, buyout_price=buyout_price,
                       region=region)


def test_proxy_bid_keeps_earlier_bidder_leading_at_lower_price():
    p = make()
    listed(p)
    p.place_bid("a1", "alice", 100, maximum=500, now=0)
    out = p.place_bid("a1", "bob", 120, now=1)
    assert out["status"] == "outbid"
    assert out["leader"] == "alice"
    assert out["price"] == 120
    later = p.place_bid("a1", "carol", 300, now=2)
    assert later["leader"] == "alice"
    assert later["price"] == 300


def test_equal_proxy_ceilings_keep_first_bidder_ahead():
    p = make()
    listed(p)
    p.place_bid("a1", "alice", 100, maximum=500, now=0)
    out = p.place_bid("a1", "bob", 100, maximum=500, now=1)
    assert out["status"] == "outbid"
    assert out["leader"] == "alice"
    assert p.get_auction("a1")["leader"] == "alice"


def test_bid_inside_extension_window_pushes_end_time():
    p = make()
    listed(p, duration=60)
    assert p.get_auction("a1")["end_time"] == 60
    p.place_bid("a1", "alice", 100, now=10)
    assert p.get_auction("a1")["end_time"] == 60
    p.place_bid("a1", "bob", 120, now=55)
    assert p.get_auction("a1")["end_time"] == 65


def test_bid_equal_to_reserve_price_sells():
    p = make()
    listed(p, reserve=100)
    p.place_bid("a1", "alice", 100, now=0)
    out = p.end_auction("a1", now=61)
    assert out["status"] == "sold"
    assert out["winner"] == "alice"
    assert out["price"] == 100


def test_second_buyout_is_rejected_and_charged_once():
    p = make()
    listed(p, buyout_price=800)
    first = p.buyout("a1", "alice", now=5)
    assert first["status"] == "sold"
    assert first["price"] == 800
    with pytest.raises(abp.AuctionError) as exc:
        p.buyout("a1", "bob", now=6)
    assert exc.value.code == "ALREADY_SOLD"
    payments = [e for e in p.ledger("a1") if e["kind"] == "payment"]
    assert len(payments) == 1


def test_cancel_refunds_deposit_exactly_once():
    p = make()
    listed(p, deposit=50)
    first = p.cancel_auction("a1", "seller-1", now=5)
    assert first["status"] == "cancelled"
    assert first["refunded"] == 50
    second = p.cancel_auction("a1", "seller-1", now=6)
    assert second["status"] == "already_cancelled"
    assert second["refunded"] == 0
    refunds = [e for e in p.ledger("a1") if e["kind"] == "deposit_refund"]
    assert len(refunds) == 1


def test_settlement_mails_item_once_without_empty_mail():
    p = make()
    listed(p, item="epic-sword")
    p.place_bid("a1", "alice", 150, now=0)
    p.end_auction("a1", now=61)
    mails = p.outbox()
    assert len(mails) == 1
    assert mails[0]["to"] == "alice"
    assert mails[0]["item_id"] == "epic-sword"


def test_clock_rollback_does_not_revive_ended_auction():
    p = make()
    listed(p)
    p.place_bid("a1", "alice", 150, now=0)
    first = p.end_auction("a1", now=61)
    replay = p.end_auction("a1", now=0)
    assert replay["status"] == "sold"
    assert replay["winner"] == first["winner"]
    assert len(p.outbox()) == 1
    with pytest.raises(abp.AuctionError) as exc:
        p.place_bid("a1", "bob", 999, now=62)
    assert exc.value.code == "AUCTION_ENDED"


def test_listing_search_is_region_scoped_without_duplicates():
    p = make()
    listed(p, aid="a1", region="s1")
    listed(p, aid="a2", region="s2")
    assert [a["auction_id"] for a in p.search("s1")] == ["a1"]
    assert [a["auction_id"] for a in p.search("s2")] == ["a2"]


def test_escrowed_item_cannot_be_withdrawn_by_seller():
    p = make()
    listed(p)
    with pytest.raises(abp.AuctionError) as exc:
        p.withdraw_item("a1")
    assert exc.value.code == "ITEM_ESCROWED"
    assert p.get_auction("a1")["item_state"] == "escrowed"


def test_ledger_accounts_sum_to_zero():
    p = make()
    listed(p)
    p.place_bid("a1", "alice", 150, now=0)
    p.end_auction("a1", now=61)
    accounts = p.accounts()
    assert sum(accounts.values()) == 0
    assert accounts["buyer"] == -150
    assert accounts["seller"] == 150


def test_mail_failure_rolls_back_settlement():
    def broken_mail(mail):
        raise RuntimeError("mail service down")

    p = make(mailer=broken_mail)
    listed(p)
    p.place_bid("a1", "alice", 150, now=0)
    with pytest.raises(abp.AuctionError) as exc:
        p.end_auction("a1", now=61)
    assert exc.value.code == "SETTLEMENT_ROLLBACK"
    assert p.get_auction("a1")["status"] == "active"
    assert [e for e in p.ledger("a1") if e["kind"] == "payment"] == []


def test_bid_below_current_price_is_rejected():
    p = make()
    listed(p)
    p.place_bid("a1", "alice", 100, now=0)
    with pytest.raises(abp.AuctionError) as exc:
        p.place_bid("a1", "bob", 50, now=1)
    assert exc.value.code == "BID_TOO_LOW"
    assert p.get_auction("a1")["price"] == 100


def test_end_before_deadline_is_refused():
    p = make()
    listed(p)
    p.place_bid("a1", "alice", 150, now=0)
    with pytest.raises(abp.AuctionError) as exc:
        p.end_auction("a1", now=59)
    assert exc.value.code == "AUCTION_NOT_ENDED"
    assert p.get_auction("a1")["status"] == "active"


def test_unsold_auction_returns_deposit_once():
    p = make()
    listed(p, reserve=1000, deposit=50)
    out = p.end_auction("a1", now=61)
    assert out["status"] == "unsold"
    refunds = [e for e in p.ledger("a1") if e["kind"] == "deposit_refund"]
    assert len(refunds) == 1
    assert refunds[0]["amount"] == 50
    assert sum(p.accounts().values()) == 0
