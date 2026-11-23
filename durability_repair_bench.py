import copy
import math
import threading
from decimal import Decimal, ROUND_HALF_UP


class RepairError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class DurabilityRepairBench:
    """Item durability, repair pricing and transactional settlement.

    The bench is deliberately self-contained: every public operation takes the
    same lock, repairs use the stored item revision as a compare-and-swap
    token, and the wallet is called before a repair is committed.  A wallet
    failure therefore leaves both the item and its revision untouched.
    """

    def __init__(self, rate_per_point=10, wallet=None):
        self.rate_per_point = rate_per_point
        self.wallet = wallet
        self._items = {}
        self._ledger = []
        self._events = []
        self._next_transaction_id = 1
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Registration and item state
    # ------------------------------------------------------------------
    def register_item(self, item_id, owner, max_durability, durability,
                      revision=1, set_id="default", equipped=True,
                      attributes=None, **kwargs):
        with self._lock:
            if item_id in self._items:
                raise RepairError("ITEM_EXISTS", item_id)
            if owner is None:
                raise RepairError("INVALID_OWNER")
            if revision is None or int(revision) < 0:
                raise RepairError("INVALID_REVISION")

            maximum = int(max_durability)
            current = int(durability)
            if maximum < 0:
                raise RepairError("INVALID_MAX_DURABILITY")
            current = min(max(0, current), maximum)

            self._items[item_id] = {
                "item_id": item_id,
                "owner": owner,
                "max_durability": maximum,
                "durability": current,
                "revision": int(revision),
                "set_id": set_id,
                "equipped": bool(equipped),
                "listed": False,
                "attributes": copy.deepcopy(attributes) if attributes else None,
            }
            return self.snapshot(item_id)

    def set_max_durability(self, item_id, new_max):
        with self._lock:
            item = self._get_item(item_id)
            try:
                maximum = int(new_max)
            except (TypeError, ValueError):
                raise RepairError("INVALID_MAX_DURABILITY")
            if maximum < 0:
                raise RepairError("INVALID_MAX_DURABILITY")

            changed = maximum != item["max_durability"]
            item["max_durability"] = maximum
            if item["durability"] > maximum:
                item["durability"] = maximum
                changed = True
            if changed:
                item["revision"] += 1
            return self.snapshot(item_id)

    def snapshot(self, item_id):
        with self._lock:
            item = self._get_item(item_id)
            result = copy.deepcopy(item)
            result["broken"] = result["durability"] <= 0
            return result

    def dump(self):
        with self._lock:
            return {
                "version": 1,
                "rate_per_point": self.rate_per_point,
                "items": copy.deepcopy(self._items),
                "ledger": copy.deepcopy(self._ledger),
                "events": copy.deepcopy(self._events),
                "next_transaction_id": self._next_transaction_id,
            }

    def load(self, state):
        if not isinstance(state, dict):
            raise RepairError("INVALID_STATE")

        items = state.get("items", {})
        ledger = state.get("ledger", [])
        events = state.get("events", [])
        if not isinstance(items, dict) or not isinstance(ledger, list):
            raise RepairError("INVALID_STATE")

        restored_items = {}
        for item_id, raw in items.items():
            if not isinstance(raw, dict):
                raise RepairError("INVALID_STATE")
            item = copy.deepcopy(raw)
            item.setdefault("item_id", item_id)
            item.setdefault("equipped", True)
            item.setdefault("listed", False)
            item.setdefault("attributes", None)
            maximum = int(item["max_durability"])
            durability = min(max(0, int(item["durability"])), maximum)
            item["max_durability"] = maximum
            item["durability"] = durability
            item["revision"] = int(item["revision"])
            restored_items[item_id] = item

        with self._lock:
            self.rate_per_point = state.get(
                "rate_per_point", self.rate_per_point)
            self._items = restored_items
            self._ledger = copy.deepcopy(ledger)
            self._events = copy.deepcopy(events)
            self._next_transaction_id = int(
                state.get("next_transaction_id", len(self._ledger) + 1))
        return self

    # ------------------------------------------------------------------
    # Pricing and repair
    # ------------------------------------------------------------------
    def quote(self, item_id, discount=0.0, discount_source=None):
        with self._lock:
            item = self._get_item(item_id)
            rate = self._validate_discount(discount)
            cost = self._price(item, rate)
            return {
                "item_id": item_id,
                "cost": cost,
                "discount": rate,
                "discount_source": self._discount_source(rate, discount_source),
                "durability": item["durability"],
                "max_durability": item["max_durability"],
                "missing_durability": max(
                    0, item["max_durability"] - item["durability"]),
                "rate_per_point": self.rate_per_point,
                "currency": "copper",
            }

    def repair(self, item_id, actor, revision, discount=0.0, in_combat=False,
               now=0, discount_source=None, **kwargs):
        with self._lock:
            return self._repair_locked(
                item_id, actor, revision, discount=discount,
                in_combat=in_combat, now=now,
                discount_source=discount_source)

    def _repair_locked(self, item_id, actor, revision, discount=0.0,
                       in_combat=False, now=0, discount_source=None):
        item = self._get_item(item_id)

        if item["owner"] != actor:
            raise RepairError("NOT_OWNER", item_id)
        if revision is None or int(revision) != item["revision"]:
            raise RepairError("STALE_REVISION", item["revision"])
        if in_combat:
            raise RepairError("IN_COMBAT")
        if not item["equipped"]:
            raise RepairError("NOT_EQUIPPED")
        if item["listed"]:
            raise RepairError("ITEM_LISTED")

        rate = self._validate_discount(discount)
        before = item["durability"]
        target = item["max_durability"]
        missing = max(0, target - before)
        source = self._discount_source(rate, discount_source)

        if missing <= 0:
            return {
                "item_id": item_id,
                "cost": 0,
                "charged": False,
                "repaired": False,
                "durability": before,
                "before_durability": before,
                "after_durability": before,
                "revision": item["revision"],
                "discount": rate,
                "discount_source": source,
            }

        cost = self._price(item, rate)
        # The client-provided amount (if any) is intentionally ignored.  The
        # amount above is derived from the server's current snapshot.
        self._charge(cost)

        revision_before = item["revision"]
        revision_after = revision_before + 1
        # Build the committed image before mutating the live object.  A
        # successful payment either commits all of these fields or none.
        committed = copy.deepcopy(item)
        committed["durability"] = target
        committed["revision"] = revision_after
        entry = {
            "transaction_id": self._next_transaction_id,
            "kind": "repair",
            "item_id": item_id,
            "actor": actor,
            "owner": item["owner"],
            "before_durability": before,
            "after_durability": target,
            "durability_before": before,
            "durability_after": target,
            "cost": cost,
            "discount": rate,
            "discount_source": source,
            "revision_before": revision_before,
            "revision_after": revision_after,
            "timestamp": now,
        }
        new_ledger = self._ledger + [entry]

        self._items[item_id] = committed
        self._ledger = new_ledger
        self._next_transaction_id += 1
        return {
            "item_id": item_id,
            "cost": cost,
            "charged": cost > 0,
            "repaired": True,
            "durability": target,
            "before_durability": before,
            "after_durability": target,
            "revision": revision_after,
            "discount": rate,
            "discount_source": source,
            "transaction_id": entry["transaction_id"],
        }

    def repair_all(self, character, set_id=None):
        with self._lock:
            results = []
            repaired = 0
            total_cost = 0
            for item_id, item in list(self._items.items()):
                if item["owner"] != character:
                    continue
                if set_id is not None and item["set_id"] != set_id:
                    continue
                if not item["equipped"] or item["listed"]:
                    continue
                out = self._repair_locked(
                    item_id, character, item["revision"], discount=0.0)
                results.append(out)
                if out["repaired"]:
                    repaired += 1
                total_cost += out["cost"]
            return {
                "actor": character,
                "set_id": set_id,
                "repaired": repaired,
                "cost": total_cost,
                "items": results,
                "results": results,
            }

    # ------------------------------------------------------------------
    # Events and ownership
    # ------------------------------------------------------------------
    def apply_death_wear(self, item_id, amount=5):
        with self._lock:
            item = self._get_item(item_id)
            try:
                wear = int(amount)
            except (TypeError, ValueError):
                raise RepairError("INVALID_WEAR_AMOUNT")
            if wear < 0:
                raise RepairError("INVALID_WEAR_AMOUNT")

            before = item["durability"]
            after = max(0, before - wear)
            if after != before:
                item["durability"] = after
                item["revision"] += 1
                self._events.append({
                    "kind": "death_wear",
                    "item_id": item_id,
                    "before_durability": before,
                    "after_durability": after,
                    "amount": wear,
                })
            return {
                "item_id": item_id,
                "durability": after,
                "before_durability": before,
                "after_durability": after,
                "revision": item["revision"],
            }

    def trade(self, item_id, new_owner):
        with self._lock:
            item = self._get_item(item_id)
            if new_owner is None:
                raise RepairError("INVALID_OWNER")
            if new_owner == item["owner"]:
                return self.snapshot(item_id)

            item["owner"] = new_owner
            item["listed"] = False
            item["revision"] += 1
            return self.snapshot(item_id)

    def list_for_auction(self, item_id):
        with self._lock:
            item = self._get_item(item_id)
            if item["durability"] <= 0:
                raise RepairError("ITEM_BROKEN", item_id)
            if item["listed"]:
                raise RepairError("ALREADY_LISTED", item_id)
            item["listed"] = True
            return {
                "item_id": item_id,
                "listed": True,
                "revision": item["revision"],
            }

    def attributes(self, item_id):
        with self._lock:
            item = self._get_item(item_id)
            if item["durability"] <= 0:
                return {}
            if item["attributes"] is not None:
                return copy.deepcopy(item["attributes"])
            return {
                "durability": item["durability"],
                "max_durability": item["max_durability"],
            }

    # ------------------------------------------------------------------
    # Ledger helpers
    # ------------------------------------------------------------------
    @property
    def ledger(self):
        with self._lock:
            return copy.deepcopy(self._ledger)

    @property
    def transactions(self):
        return self.ledger

    def history(self, item_id=None):
        with self._lock:
            entries = copy.deepcopy(self._ledger)
        if item_id is None:
            return entries
        return [entry for entry in entries if entry["item_id"] == item_id]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _get_item(self, item_id):
        try:
            return self._items[item_id]
        except KeyError:
            raise RepairError("ITEM_NOT_FOUND", item_id)

    @staticmethod
    def _validate_discount(discount):
        try:
            rate = float(discount)
        except (TypeError, ValueError):
            raise RepairError("INVALID_DISCOUNT")
        if math.isnan(rate) or math.isinf(rate) or not 0.0 <= rate <= 1.0:
            raise RepairError("INVALID_DISCOUNT")
        return rate

    @staticmethod
    def _discount_source(rate, source):
        if source is not None:
            return str(source)
        return "none" if rate == 0.0 else "unspecified"

    def _price(self, item, discount):
        missing = max(0, item["max_durability"] - item["durability"])
        raw = float(self.rate_per_point) * missing * (1.0 - discount)
        return max(0, int(Decimal(str(raw)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP)))

    def _charge(self, amount):
        if amount <= 0 or self.wallet is None:
            return
        try:
            if callable(self.wallet):
                paid = self.wallet(amount)
            elif hasattr(self.wallet, "debit"):
                paid = self.wallet.debit(amount)
            else:
                raise TypeError("wallet is not callable and has no debit()")
        except RepairError:
            raise
        except Exception as exc:
            raise RepairError("PAYMENT_FAILED", str(exc)) from exc
        if not paid:
            raise RepairError("PAYMENT_FAILED")
