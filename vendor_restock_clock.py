import threading


class VendorError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VendorRestockClock:
    """Vendor stock, purchase limits and absolute restock schedule."""

    def __init__(self, wallet=None):
        self.wallet = wallet
        self._lock = threading.RLock()
        self._items = {}
        self._outbox = []

    @staticmethod
    def _key(vendor_id, item_id):
        return vendor_id, item_id

    def _record(self, vendor_id, item_id):
        record = self._items.get(self._key(vendor_id, item_id))
        if record is None:
            raise VendorError("ITEM_UNAVAILABLE")
        return record

    @staticmethod
    def _check_stock_bounds(stock, cap):
        if stock < 0 or stock > cap:
            raise VendorError("INVALID_STOCK", {"stock": stock, "cap": cap})

    def add_item(self, vendor_id, item_id, stock, cap, restock_interval,
                 next_restock_at, purchase_limit=None, price=1):
        if cap < 0:
            raise VendorError("INVALID_CONFIGURATION", "cap must be >= 0")
        if restock_interval <= 0:
            raise VendorError("INVALID_CONFIGURATION",
                              "restock_interval must be > 0")
        self._check_stock_bounds(stock, cap)
        if purchase_limit is not None and purchase_limit < 0:
            raise VendorError("INVALID_CONFIGURATION",
                              "purchase_limit must be >= 0")
        with self._lock:
            self._items[self._key(vendor_id, item_id)] = {
                "vendor_id": vendor_id,
                "item_id": item_id,
                "stock": stock,
                "cap": cap,
                "restock_interval": restock_interval,
                "next_restock_at": next_restock_at,
                "purchase_limit": purchase_limit,
                "price": price,
                "purchases": {},
            }

    def buy(self, vendor_id, item_id, account_id, character_id, quantity,
            now=0, bag_free_slots=1):
        if quantity <= 0:
            raise VendorError("INVALID_QUANTITY", {"quantity": quantity})
        with self._lock:
            record = self._record(vendor_id, item_id)

            if bag_free_slots <= 0:
                raise VendorError("BAG_FULL", {"free": bag_free_slots})

            if record["stock"] < quantity:
                raise VendorError("OUT_OF_STOCK",
                                  {"stock": record["stock"],
                                   "requested": quantity})

            bought = record["purchases"].get(account_id, 0)
            limit = record["purchase_limit"]
            if limit is not None and bought + quantity > limit:
                raise VendorError("LIMIT_EXCEEDED",
                                  {"account": account_id,
                                   "bought": bought,
                                   "limit": limit})

            amount = record["price"] * quantity
            if self.wallet is not None and not self.wallet(amount):
                raise VendorError("PAYMENT_FAILED", {"amount": amount})

            if not self._decrement_stock(record, quantity):
                raise VendorError("OUT_OF_STOCK",
                                  {"stock": record["stock"],
                                   "requested": quantity})
            record["purchases"][account_id] = bought + quantity

            delivery = {
                "vendor_id": vendor_id,
                "item_id": item_id,
                "account_id": account_id,
                "character_id": character_id,
                "quantity": quantity,
                "at": now,
            }
            self._outbox.append(delivery)

            self._check_stock_bounds(record["stock"], record["cap"])
            return {"bought": quantity, "stock": record["stock"],
                    "delivery": dict(delivery)}

    @staticmethod
    def _decrement_stock(record, quantity):
        if record["stock"] < quantity:
            return False
        record["stock"] -= quantity
        return True

    def restock(self, vendor_id, now=0):
        restocked = []
        with self._lock:
            for key, record in self._items.items():
                if key[0] != vendor_id:
                    continue
                if record["next_restock_at"] > now:
                    continue
                while record["next_restock_at"] <= now:
                    record["next_restock_at"] += record["restock_interval"]
                record["stock"] = record["cap"]
                self._check_stock_bounds(record["stock"], record["cap"])
                restocked.append(record["item_id"])
        return {"restocked": restocked}

    def remove_item(self, vendor_id, item_id):
        with self._lock:
            self._items.pop(self._key(vendor_id, item_id), None)

    def stock(self, vendor_id, item_id):
        with self._lock:
            return self._record(vendor_id, item_id)["stock"]

    def next_restock(self, vendor_id, item_id):
        with self._lock:
            return self._record(vendor_id, item_id)["next_restock_at"]

    def account_purchases(self, vendor_id, item_id, account_id):
        with self._lock:
            record = self._record(vendor_id, item_id)
            return record["purchases"].get(account_id, 0)

    def describe(self, vendor_id, item_id):
        with self._lock:
            record = self._record(vendor_id, item_id)
            return {
                "vendor_id": record["vendor_id"],
                "item_id": record["item_id"],
                "stock": record["stock"],
                "cap": record["cap"],
                "restock_interval": record["restock_interval"],
                "next_restock_at": record["next_restock_at"],
                "purchase_limit": record["purchase_limit"],
                "price": record["price"],
            }

    def outbox(self):
        with self._lock:
            return [dict(entry) for entry in self._outbox]

    def dump(self):
        with self._lock:
            return {
                "version": 1,
                "items": [
                    {
                        "vendor_id": record["vendor_id"],
                        "item_id": record["item_id"],
                        "stock": record["stock"],
                        "cap": record["cap"],
                        "restock_interval": record["restock_interval"],
                        "next_restock_at": record["next_restock_at"],
                        "purchase_limit": record["purchase_limit"],
                        "price": record["price"],
                        "purchases": dict(record["purchases"]),
                    }
                    for record in self._items.values()
                ],
                "outbox": [dict(entry) for entry in self._outbox],
            }

    def load(self, state):
        with self._lock:
            items = {}
            for entry in state.get("items", []):
                record = dict(entry)
                record["purchases"] = dict(entry.get("purchases", {}))
                self._check_stock_bounds(record["stock"], record["cap"])
                items[self._key(record["vendor_id"],
                                 record["item_id"])] = record
            self._items = items
            self._outbox = [dict(entry) for entry in state.get("outbox", [])]
