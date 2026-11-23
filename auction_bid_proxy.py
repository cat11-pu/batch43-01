import copy
import threading


class AuctionError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuctionBidProxy:
    """Auction listing, proxy bidding, buyout, extension and settlement.

    Money is modelled as double-entry ledger records and goods as an escrow
    state on the auction.  Every mutating operation runs inside a lock and a
    transaction snapshot: an exception at any step (for example a failing mail
    service) restores the full state, so funds, stock and mail can never
    diverge.
    """

    def __init__(self, extension_window=10, extension_seconds=10, mailer=None):
        self.extension_window = extension_window
        self.extension_seconds = extension_seconds
        self.mailer = mailer
        self._auctions = {}
        self._ledger = []
        self._outbox = []
        self._refunded = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ utils
    def _snapshot(self):
        return (
            copy.deepcopy(self._auctions),
            copy.deepcopy(self._ledger),
            copy.deepcopy(self._outbox),
            copy.deepcopy(self._refunded),
        )

    def _restore(self, snapshot):
        (self._auctions, self._ledger, self._outbox,
         self._refunded) = (copy.deepcopy(part) for part in snapshot)

    def _get(self, auction_id):
        auction = self._auctions.get(auction_id)
        if auction is None:
            raise AuctionError("AUCTION_NOT_FOUND",
                               {"auction_id": auction_id})
        return auction

    def _post(self, auction_id, kind, src, dst, amount,
              src_party=None, dst_party=None):
        self._ledger.append({
            "auction_id": auction_id,
            "kind": kind,
            "from": src,
            "to": dst,
            "amount": amount,
            "from_party": src if src_party is None else src_party,
            "to_party": dst if dst_party is None else dst_party,
        })

    def _refund_deposit(self, auction):
        """Idempotent per-auction deposit refund."""
        auction_id = auction["auction_id"]
        if auction_id in self._refunded:
            return 0
        deposit = auction["deposit"]
        if deposit > 0:
            self._post(auction_id, "deposit_refund", "house",
                       auction["seller_id"], deposit,
                       src_party="house", dst_party="seller")
        self._refunded.add(auction_id)
        return deposit

    def _send_mail(self, mail):
        if self.mailer is not None:
            self.mailer(mail)
        self._outbox.append(dict(mail))

    def _settle_sold(self, auction, winner_id, price):
        auction_id = auction["auction_id"]
        self._post(auction_id, "payment", winner_id,
                   auction["seller_id"], price,
                   src_party="buyer", dst_party="seller")
        self._refund_deposit(auction)
        auction["item_state"] = "delivered"
        self._send_mail({
            "auction_id": auction_id,
            "to": winner_id,
            "item_id": auction["item_id"],
            "kind": "item",
        })

    def _settle_unsold(self, auction):
        auction_id = auction["auction_id"]
        self._refund_deposit(auction)
        auction["item_state"] = "returned"
        self._send_mail({
            "auction_id": auction_id,
            "to": auction["seller_id"],
            "item_id": auction["item_id"],
            "kind": "item_return",
        })

    # ----------------------------------------------------------------- public
    def list_item(self, auction_id, seller_id, item_id, reserve_price, duration,
                  now=0, deposit=0, buyout_price=None, region="s1"):
        with self._lock:
            if auction_id in self._auctions:
                raise AuctionError("DUPLICATE_LISTING",
                                   {"auction_id": auction_id})
            if reserve_price < 0 or duration <= 0 or deposit < 0:
                raise AuctionError("INVALID_LISTING")
            auction = {
                "auction_id": auction_id,
                "seller_id": seller_id,
                "item_id": item_id,
                "reserve_price": reserve_price,
                "buyout_price": buyout_price,
                "start_time": now,
                "end_time": now + duration,
                "duration": duration,
                "deposit": deposit,
                "region": region,
                "status": "active",
                "item_state": "escrowed",
                "bids": [],
                "leader": None,
                "price": 0,
                "result": None,
            }
            if deposit > 0:
                self._post(auction_id, "deposit", seller_id, "house", deposit,
                           src_party="seller", dst_party="house")
            self._auctions[auction_id] = auction
            return self.get_auction(auction_id)

    def place_bid(self, auction_id, bidder_id, amount, maximum=None, now=0):
        with self._lock:
            snapshot = self._snapshot()
            try:
                auction = self._get(auction_id)
                if auction["status"] != "active":
                    raise AuctionError("AUCTION_ENDED",
                                       {"auction_id": auction_id})
                if now >= auction["end_time"]:
                    raise AuctionError("AUCTION_ENDED",
                                       {"auction_id": auction_id})
                ceiling = maximum if maximum is not None else amount
                if amount < auction["reserve_price"]:
                    raise AuctionError(
                        "BID_TOO_LOW",
                        {"reserve_price": auction["reserve_price"]})
                if ceiling < amount:
                    raise AuctionError("BID_TOO_LOW")

                leader = auction["leader"]
                existing = next((bid for bid in auction["bids"]
                                 if bid["bidder_id"] == bidder_id), None)
                is_new_bidder = existing is None
                if existing is not None:
                    if ceiling <= existing["maximum"]:
                        raise AuctionError("BID_TOO_LOW")
                    existing["maximum"] = ceiling
                    existing["amount"] = amount
                else:
                    if leader is not None and ceiling <= auction["price"]:
                        raise AuctionError(
                            "BID_TOO_LOW",
                            {"current_price": auction["price"]})
                    auction["bids"].append({
                        "bidder_id": bidder_id,
                        "amount": amount,
                        "maximum": ceiling,
                        "seq": len(auction["bids"]),
                    })

                if leader is None:
                    auction["leader"] = bidder_id
                    auction["price"] = amount
                    status = "leading"
                elif leader == bidder_id:
                    status = "leading"
                else:
                    leader_bid = next(bid for bid in auction["bids"]
                                      if bid["bidder_id"] == leader)
                    leader_ceiling = leader_bid["maximum"]
                    if ceiling > leader_ceiling:
                        if is_new_bidder and amount <= auction["price"]:
                            raise AuctionError(
                                "BID_TOO_LOW",
                                {"current_price": auction["price"]})
                        auction["leader"] = bidder_id
                        auction["price"] = max(amount, leader_ceiling)
                        status = "leading"
                    else:
                        # Tie (equal ceilings) keeps the earlier bidder in
                        # front; the price is raised up to the challenger's
                        # proxy ceiling.
                        if ceiling > auction["price"]:
                            auction["price"] = ceiling
                        status = "outbid"

                if now >= auction["end_time"] - self.extension_window:
                    auction["end_time"] = max(
                        auction["end_time"],
                        now + self.extension_seconds)

                return {
                    "auction_id": auction_id,
                    "status": status,
                    "leader": auction["leader"],
                    "price": auction["price"],
                    "end_time": auction["end_time"],
                }
            except AuctionError:
                self._restore(snapshot)
                raise
            except Exception as exc:
                self._restore(snapshot)
                raise AuctionError("BID_ROLLBACK", str(exc)) from exc

    def buyout(self, auction_id, buyer_id, now=0):
        with self._lock:
            snapshot = self._snapshot()
            try:
                auction = self._get(auction_id)
                if auction["status"] != "active":
                    raise AuctionError("ALREADY_SOLD",
                                       {"status": auction["status"]})
                if now >= auction["end_time"]:
                    raise AuctionError("AUCTION_ENDED")
                buyout_price = auction["buyout_price"]
                if buyout_price is None:
                    raise AuctionError("NO_BUYOUT")
                self._settle_sold(auction, buyer_id, buyout_price)
                auction["status"] = "sold"
                auction["result"] = {
                    "status": "sold",
                    "winner": buyer_id,
                    "price": buyout_price,
                    "via": "buyout",
                }
                return dict(auction["result"])
            except AuctionError:
                self._restore(snapshot)
                raise
            except Exception as exc:
                self._restore(snapshot)
                raise AuctionError("SETTLEMENT_ROLLBACK", str(exc)) from exc

    def cancel_auction(self, auction_id, actor, now=0):
        with self._lock:
            snapshot = self._snapshot()
            try:
                auction = self._get(auction_id)
                if auction["status"] == "cancelled":
                    return {"status": "already_cancelled", "refunded": 0}
                if auction["status"] != "active":
                    raise AuctionError("AUCTION_ENDED",
                                       {"status": auction["status"]})
                if actor != auction["seller_id"]:
                    raise AuctionError("NOT_SELLER")

                had_bids = auction["leader"] is not None
                refunded = self._refund_deposit(auction)
                penalty = 0
                if had_bids and auction["deposit"] > 0:
                    penalty = auction["deposit"]
                    self._post(auction_id, "penalty",
                               auction["seller_id"], "house", penalty,
                               src_party="seller", dst_party="house")
                auction["item_state"] = "returned"
                self._send_mail({
                    "auction_id": auction_id,
                    "to": auction["seller_id"],
                    "item_id": auction["item_id"],
                    "kind": "item_return",
                })
                auction["status"] = "cancelled"
                auction["result"] = {
                    "status": "cancelled",
                    "refunded": refunded,
                    "penalty": penalty,
                }
                return dict(auction["result"])
            except AuctionError:
                self._restore(snapshot)
                raise
            except Exception as exc:
                self._restore(snapshot)
                raise AuctionError("SETTLEMENT_ROLLBACK", str(exc)) from exc

    def end_auction(self, auction_id, now=0):
        with self._lock:
            auction = self._get(auction_id)
            if auction["status"] != "active":
                # Clock rollback must never revive a finished auction:
                # replay returns the recorded result instead.
                return dict(auction["result"])
            snapshot = self._snapshot()
            try:
                if now < auction["end_time"]:
                    raise AuctionError(
                        "AUCTION_NOT_ENDED",
                        {"end_time": auction["end_time"]})
                if auction["leader"] is not None:
                    winner = auction["leader"]
                    price = auction["price"]
                    self._settle_sold(auction, winner, price)
                    auction["status"] = "sold"
                    auction["result"] = {
                        "status": "sold",
                        "winner": winner,
                        "price": price,
                        "via": "bid",
                    }
                else:
                    self._settle_unsold(auction)
                    auction["status"] = "unsold"
                    auction["result"] = {"status": "unsold"}
                return dict(auction["result"])
            except AuctionError:
                self._restore(snapshot)
                raise
            except Exception as exc:
                self._restore(snapshot)
                raise AuctionError("SETTLEMENT_ROLLBACK", str(exc)) from exc

    def get_auction(self, auction_id):
        with self._lock:
            auction = self._get(auction_id)
            return {
                "auction_id": auction["auction_id"],
                "seller_id": auction["seller_id"],
                "item_id": auction["item_id"],
                "reserve_price": auction["reserve_price"],
                "buyout_price": auction["buyout_price"],
                "start_time": auction["start_time"],
                "end_time": auction["end_time"],
                "deposit": auction["deposit"],
                "region": auction["region"],
                "status": auction["status"],
                "item_state": auction["item_state"],
                "leader": auction["leader"],
                "price": auction["price"],
                "bid_count": len(auction["bids"]),
            }

    def ledger(self, auction_id=None):
        with self._lock:
            if auction_id is None:
                return [dict(entry) for entry in self._ledger]
            return [dict(entry) for entry in self._ledger
                    if entry["auction_id"] == auction_id]

    def accounts(self):
        with self._lock:
            balances = {}
            for entry in self._ledger:
                amount = entry["amount"]
                src = entry.get("from_party", entry["from"])
                dst = entry.get("to_party", entry["to"])
                balances[src] = balances.get(src, 0) - amount
                balances[dst] = balances.get(dst, 0) + amount
            return balances

    def outbox(self):
        with self._lock:
            return [dict(mail) for mail in self._outbox]

    def search(self, region):
        with self._lock:
            return [self.get_auction(auction_id)
                    for auction_id, auction in self._auctions.items()
                    if auction["region"] == region
                    and auction["status"] == "active"]

    def withdraw_item(self, auction_id):
        with self._lock:
            auction = self._get(auction_id)
            if auction["item_state"] == "escrowed":
                raise AuctionError("ITEM_ESCROWED",
                                   {"item_id": auction["item_id"]})
            return {"auction_id": auction_id,
                    "item_state": auction["item_state"]}
