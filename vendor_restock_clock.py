class VendorError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VendorRestockClock:
    """Vendor stock, purchase limits and absolute restock schedule."""

    def __init__(self, wallet=None):
        self.wallet = wallet

    def add_item(self, vendor_id, item_id, stock, cap, restock_interval,
                 next_restock_at, purchase_limit=None, price=1):
        raise NotImplementedError

    def buy(self, vendor_id, item_id, account_id, character_id, quantity,
            now=0, bag_free_slots=1):
        raise NotImplementedError

    def restock(self, vendor_id, now=0):
        raise NotImplementedError

    def remove_item(self, vendor_id, item_id):
        raise NotImplementedError

    def stock(self, vendor_id, item_id):
        raise NotImplementedError

    def next_restock(self, vendor_id, item_id):
        raise NotImplementedError

    def account_purchases(self, vendor_id, item_id, account_id):
        raise NotImplementedError

    def describe(self, vendor_id, item_id):
        raise NotImplementedError

    def outbox(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
