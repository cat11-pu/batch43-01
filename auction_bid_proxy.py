class AuctionError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuctionBidProxy:
    """Auction listing, proxy bidding, buyout, extension and settlement."""

    def __init__(self, extension_window=10, extension_seconds=10, mailer=None):
        self.extension_window = extension_window
        self.extension_seconds = extension_seconds
        self.mailer = mailer

    def list_item(self, auction_id, seller_id, item_id, reserve_price, duration,
                  now=0, deposit=0, buyout_price=None, region="s1"):
        raise NotImplementedError

    def place_bid(self, auction_id, bidder_id, amount, maximum=None, now=0):
        raise NotImplementedError

    def buyout(self, auction_id, buyer_id, now=0):
        raise NotImplementedError

    def cancel_auction(self, auction_id, actor, now=0):
        raise NotImplementedError

    def end_auction(self, auction_id, now=0):
        raise NotImplementedError

    def get_auction(self, auction_id):
        raise NotImplementedError

    def ledger(self, auction_id=None):
        raise NotImplementedError

    def accounts(self):
        raise NotImplementedError

    def outbox(self):
        raise NotImplementedError

    def search(self, region):
        raise NotImplementedError

    def withdraw_item(self, auction_id):
        raise NotImplementedError
