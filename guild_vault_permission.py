import copy
import threading
import time


class VaultError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class GuildVaultPermission:
    """Guild vault members, ranks, inventory and audit log.

    The three mutable domains are intentionally kept separate:

    * ``_members`` owns identities/accounts and their current rank.
    * ``_permissions`` owns rank limits and per-slot access rules.
    * ``_inventory`` owns item stacks.

    Permission checks always read the current member rank from ``_members``
    while holding ``_lock``; there is no session-level permission cache.
    """

    DAY_SECONDS = 24 * 60 * 60
    DEFAULT_WITHDRAW_RANK = 3

    def __init__(self, default_daily_limit=None):
        self.default_daily_limit = default_daily_limit

        self._members = {}
        self._permissions = {}
        self._inventory = {}
        self._leaders = {}
        self._daily_usage = {}
        self._logs = {}
        self._outbox = []
        self._disbanded = set()

        # A vault operation is an in-memory transaction.  The re-entrant lock
        # makes permission, quota and stock validation plus mutation atomic.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add_member(self, guild_id, character_id, rank, account_id=None):
        with self._lock:
            rank = self._rank_value(rank)
            members = self._members_for(guild_id)
            if character_id in members:
                raise VaultError(
                    "MEMBER_EXISTS",
                    {"guild_id": guild_id, "character_id": character_id},
                )

            member = {
                "character_id": character_id,
                "rank": rank,
                "account_id": account_id,
            }
            members[character_id] = member
            self._disbanded.discard(guild_id)
            if rank >= 5 and guild_id not in self._leaders:
                self._leaders[guild_id] = character_id
            return copy.deepcopy(member)

    def set_rank(self, guild_id, character_id, rank):
        with self._lock:
            rank = self._rank_value(rank)
            member = self._member_or_error(guild_id, character_id)
            member["rank"] = rank
            if rank >= 5 and guild_id not in self._leaders:
                self._leaders[guild_id] = character_id
            return copy.deepcopy(member)

    def set_daily_limit(self, guild_id, rank, limit):
        with self._lock:
            rank = self._rank_value(rank)
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
            ):
                raise VaultError(
                    "INVALID_LIMIT",
                    {"guild_id": guild_id, "rank": rank, "limit": limit},
                )
            permissions = self._permissions_for(guild_id)
            permissions["daily_limits"][rank] = limit
            return copy.deepcopy(
                {"guild_id": guild_id, "rank": rank, "limit": limit}
            )

    def add_slot(
        self,
        guild_id,
        slot,
        item_id,
        quantity,
        bound=False,
        locked=False,
        min_rank=0,
    ):
        with self._lock:
            slot = self._slot_value(slot)
            quantity = self._non_negative_quantity(quantity)
            min_rank = self._rank_value(min_rank)
            if item_id is None or item_id == "":
                raise VaultError(
                    "INVALID_ITEM",
                    {"guild_id": guild_id, "slot": slot, "item_id": item_id},
                )

            inventory = self._inventory_for(guild_id)
            permissions = self._permissions_for(guild_id)
            if slot in inventory:
                raise VaultError(
                    "SLOT_OCCUPIED", {"guild_id": guild_id, "slot": slot}
                )

            inventory[slot] = {
                "item_id": item_id,
                "quantity": quantity,
                "bound": bool(bound),
            }
            permissions["slots"][slot] = {
                "locked": bool(locked),
                "min_rank": min_rank,
            }
            self._disbanded.discard(guild_id)
            return self._slot_copy(guild_id, slot)

    def deposit(
        self,
        guild_id,
        character_id,
        slot,
        item_id,
        quantity,
        bound=False,
        now=0,
    ):
        with self._lock:
            member = self._member_or_error(guild_id, character_id)
            slot = self._slot_value(slot)
            quantity = self._positive_quantity(quantity)
            if item_id is None or item_id == "":
                raise VaultError(
                    "INVALID_ITEM",
                    {
                        "guild_id": guild_id,
                        "character_id": character_id,
                        "slot": slot,
                        "item_id": item_id,
                    },
                )

            inventory = self._inventory.get(guild_id, {})
            if slot not in inventory:
                raise VaultError(
                    "SLOT_NOT_FOUND", {"guild_id": guild_id, "slot": slot}
                )

            stack = inventory[slot]
            incoming_bound = bool(bound)
            if stack["quantity"] == 0:
                # Binding belongs to the item stack, not permanently to the
                # slot.  An emptied slot may receive a different item.
                stack["item_id"] = item_id
                stack["bound"] = incoming_bound
            else:
                if stack["item_id"] != item_id:
                    raise VaultError(
                        "ITEM_MISMATCH",
                        {
                            "guild_id": guild_id,
                            "slot": slot,
                            "expected": stack["item_id"],
                            "actual": item_id,
                        },
                    )
                if bool(stack["bound"]) != incoming_bound:
                    raise VaultError(
                        "BOUND_MISMATCH",
                        {
                            "guild_id": guild_id,
                            "slot": slot,
                            "stored_bound": bool(stack["bound"]),
                            "incoming_bound": incoming_bound,
                        },
                    )

            before = stack["quantity"]
            stack["quantity"] += quantity
            self._record_log(
                guild_id,
                action="deposit",
                character_id=character_id,
                account_id=self._account_id(member),
                slot=slot,
                item_id=item_id,
                quantity=quantity,
                bound=stack["bound"],
                before_quantity=before,
                after_quantity=stack["quantity"],
                now=now,
            )
            return self._slot_copy(guild_id, slot)

    def withdraw(self, guild_id, character_id, slot, quantity, now=0):
        with self._lock:
            member = self._member_or_error(guild_id, character_id)
            slot = self._slot_value(slot)
            quantity = self._positive_quantity(quantity)

            inventory = self._inventory.get(guild_id, {})
            if slot not in inventory:
                raise VaultError(
                    "SLOT_NOT_FOUND", {"guild_id": guild_id, "slot": slot}
                )

            # Permission is checked against the current member record, before
            # quota or stock.  This keeps a kicked/demoted session from even
            # learning whether an item is available.
            self._check_slot_permission(guild_id, member, slot)

            rank = member["rank"]
            limit = self._daily_limit(guild_id, rank)
            account_id = self._account_id(member)
            day = self._day(now)
            account_usage = (
                self._daily_usage.get(guild_id, {}).get(account_id)
            )
            used = (
                account_usage["quantity"]
                if account_usage is not None and account_usage["day"] == day
                else 0
            )
            if limit is not None and used + quantity > limit:
                raise VaultError(
                    "DAILY_LIMIT_EXCEEDED",
                    {
                        "guild_id": guild_id,
                        "character_id": character_id,
                        "account_id": account_id,
                        "limit": limit,
                        "used": used,
                        "requested": quantity,
                    },
                )

            stack = inventory[slot]
            if stack["quantity"] <= 0 or quantity > stack["quantity"]:
                raise VaultError(
                    "OUT_OF_STOCK",
                    {
                        "guild_id": guild_id,
                        "slot": slot,
                        "available": stack["quantity"],
                        "requested": quantity,
                    },
                )

            before = stack["quantity"]
            stack["quantity"] -= quantity

            # Only commit account usage after every validation has passed.
            guild_usage = self._daily_usage.setdefault(guild_id, {})
            guild_usage[account_id] = {
                "day": day,
                "quantity": used + quantity,
            }

            item = {
                "guild_id": guild_id,
                "character_id": character_id,
                "account_id": account_id,
                "slot": slot,
                "item_id": stack["item_id"],
                "quantity": quantity,
                "bound": bool(stack["bound"]),
                "remaining": stack["quantity"],
            }
            self._record_log(
                guild_id,
                action="withdraw",
                character_id=character_id,
                account_id=account_id,
                slot=slot,
                item_id=stack["item_id"],
                quantity=quantity,
                bound=stack["bound"],
                before_quantity=before,
                after_quantity=stack["quantity"],
                now=now,
            )
            return item

    def expand(self, guild_id, slots, template_slot=0):
        with self._lock:
            template_slot = self._slot_value(template_slot)
            permissions = self._permissions_for(guild_id)
            if template_slot not in permissions["slots"]:
                raise VaultError(
                    "SLOT_NOT_FOUND",
                    {"guild_id": guild_id, "slot": template_slot},
                )

            template = permissions["slots"][template_slot]
            inventory = self._inventory_for(guild_id)

            if isinstance(slots, bool):
                raise VaultError("INVALID_SLOT_COUNT", {"slots": slots})
            if isinstance(slots, int):
                if slots <= 0:
                    raise VaultError("INVALID_SLOT_COUNT", {"slots": slots})
                next_slot = max(inventory, default=-1) + 1
                new_slots = list(range(next_slot, next_slot + slots))
            else:
                try:
                    new_slots = [self._slot_value(value) for value in slots]
                except TypeError:
                    raise VaultError("INVALID_SLOT_COUNT", {"slots": slots})
                if not new_slots:
                    raise VaultError("INVALID_SLOT_COUNT", {"slots": slots})

            for slot in new_slots:
                if slot in inventory:
                    raise VaultError(
                        "SLOT_OCCUPIED", {"guild_id": guild_id, "slot": slot}
                    )

            for slot in new_slots:
                inventory[slot] = {
                    "item_id": None,
                    "quantity": 0,
                    "bound": False,
                }
                permissions["slots"][slot] = {
                    "locked": bool(template.get("locked", False)),
                    "min_rank": self._rank_value(template.get("min_rank", 0)),
                }

            return [self._slot_copy(guild_id, slot) for slot in new_slots]

    def kick_member(self, guild_id, character_id):
        with self._lock:
            member = self._member_or_error(guild_id, character_id)
            if self._leaders.get(guild_id) == character_id:
                raise VaultError(
                    "CANNOT_KICK_LEADER",
                    {"guild_id": guild_id, "character_id": character_id},
                )

            outstanding = self._outstanding_for(guild_id, character_id)
            # The material/inventory return is represented by the outstanding
            # report so the caller can deduct it from the character's bags.
            # Those deductions must happen before the membership row is gone.
            for item in outstanding:
                self._record_log(
                    guild_id,
                    action="kick_clawback",
                    character_id=character_id,
                    account_id=self._account_id(member),
                    slot=None,
                    item_id=item["item_id"],
                    quantity=item["quantity"],
                    bound=item["bound"],
                    now=time.time(),
                )

            del self._members[guild_id][character_id]
            return {
                "guild_id": guild_id,
                "character_id": character_id,
                "removed": True,
                "outstanding": copy.deepcopy(outstanding),
            }

    def transfer_leader(self, guild_id, new_leader):
        with self._lock:
            members = self._members_for(guild_id)
            old_leader = self._leaders.get(guild_id)

            if new_leader not in members:
                members[new_leader] = {
                    "character_id": new_leader,
                    "rank": 5,
                    "account_id": None,
                }
            else:
                members[new_leader]["rank"] = 5

            if (
                old_leader is not None
                and old_leader != new_leader
                and old_leader in members
            ):
                # Keep the former leader in the guild, but remove all leader
                # rank privileges immediately.
                members[old_leader]["rank"] = 0

            self._leaders[guild_id] = new_leader
            self._disbanded.discard(guild_id)
            return {
                "guild_id": guild_id,
                "old_leader": old_leader,
                "new_leader": new_leader,
            }

    def disband(self, guild_id, actor=None):
        with self._lock:
            if guild_id in self._disbanded:
                return {"guild_id": guild_id, "mailed": 0, "items": []}

            leader = self._leaders.get(guild_id)
            if actor is not None:
                leader_member = self._members.get(guild_id, {}).get(leader)
                if (
                    actor != leader
                    or leader_member is None
                    or leader_member["rank"] < 5
                ):
                    raise VaultError(
                        "NOT_LEADER",
                        {
                            "guild_id": guild_id,
                            "actor": actor,
                            "leader": leader,
                        },
                    )

            inventory = self._inventory.get(guild_id, {})
            recipient = leader
            mailed_items = []
            created_at = int(time.time())

            for slot in sorted(inventory):
                stack = inventory[slot]
                if stack["quantity"] <= 0:
                    continue
                mail = {
                    "type": "guild_disband_return",
                    "guild_id": guild_id,
                    "character_id": recipient,
                    "recipient": recipient,
                    "slot": slot,
                    "item_id": stack["item_id"],
                    "quantity": stack["quantity"],
                    "bound": bool(stack["bound"]),
                    "timestamp": created_at,
                }
                mailed_items.append(copy.deepcopy(mail))
                self._outbox.append(mail)
                self._record_log(
                    guild_id,
                    action="disband_return",
                    character_id=recipient,
                    account_id=None,
                    slot=slot,
                    item_id=stack["item_id"],
                    quantity=stack["quantity"],
                    bound=stack["bound"],
                    now=created_at,
                )

            # Inventory is mailed first; only then are membership and vault
            # records deleted.
            self._members.pop(guild_id, None)
            self._permissions.pop(guild_id, None)
            self._inventory.pop(guild_id, None)
            self._leaders.pop(guild_id, None)
            self._disbanded.add(guild_id)
            return {
                "guild_id": guild_id,
                "mailed": len(mailed_items),
                "items": mailed_items,
            }

    def vault(self, guild_id):
        with self._lock:
            inventory = self._inventory.get(guild_id, {})
            if not inventory:
                return []
            # Callers use the slot number as the list index.  Keep gaps as
            # explicit empty slot records so ``vault[slot]`` is stable.
            return [
                self._slot_copy(guild_id, slot)
                if slot in inventory
                else self._empty_slot_copy(slot)
                for slot in range(max(inventory) + 1)
            ]

    def log(self, guild_id):
        with self._lock:
            return copy.deepcopy(self._logs.get(guild_id, []))

    def outbox(self):
        with self._lock:
            return copy.deepcopy(self._outbox)

    def dump(self):
        with self._lock:
            return copy.deepcopy(
                {
                    "default_daily_limit": self.default_daily_limit,
                    "members": self._members,
                    "permissions": self._permissions,
                    "inventory": self._inventory,
                    "leaders": self._leaders,
                    "daily_usage": self._daily_usage,
                    "logs": self._logs,
                    "outbox": self._outbox,
                    "disbanded": sorted(self._disbanded),
                }
            )

    def load(self, state):
        with self._lock:
            self.default_daily_limit = state.get("default_daily_limit")
            self._members = copy.deepcopy(state.get("members", {}))
            self._permissions = copy.deepcopy(state.get("permissions", {}))
            self._inventory = copy.deepcopy(state.get("inventory", {}))
            self._leaders = copy.deepcopy(state.get("leaders", {}))
            self._daily_usage = copy.deepcopy(state.get("daily_usage", {}))
            self._logs = copy.deepcopy(state.get("logs", {}))
            self._outbox = copy.deepcopy(state.get("outbox", []))
            self._disbanded = set(state.get("disbanded", []))

            for permissions in self._permissions.values():
                permissions.setdefault("daily_limits", {})
                permissions.setdefault("slots", {})
                permissions["daily_limits"] = {
                    int(rank): limit
                    for rank, limit in permissions["daily_limits"].items()
                }
                permissions["slots"] = {
                    int(slot): permission
                    for slot, permission in permissions["slots"].items()
                }
            self._inventory = {
                guild_id: {
                    int(slot): stack
                    for slot, stack in inventory.items()
                }
                for guild_id, inventory in self._inventory.items()
            }
            return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _members_for(self, guild_id):
        return self._members.setdefault(guild_id, {})

    def _permissions_for(self, guild_id):
        permissions = self._permissions.setdefault(guild_id, {})
        permissions.setdefault("daily_limits", {})
        permissions.setdefault("slots", {})
        return permissions

    def _inventory_for(self, guild_id):
        return self._inventory.setdefault(guild_id, {})

    def _member_or_error(self, guild_id, character_id):
        member = self._members.get(guild_id, {}).get(character_id)
        if member is None:
            raise VaultError(
                "MEMBER_NOT_FOUND",
                {"guild_id": guild_id, "character_id": character_id},
            )
        return member

    def _check_slot_permission(self, guild_id, member, slot):
        permission = self._permissions.get(guild_id, {}).get("slots", {}).get(slot)
        if permission is None:
            raise VaultError(
                "SLOT_NOT_FOUND", {"guild_id": guild_id, "slot": slot}
            )

        locked = bool(permission.get("locked", False))
        configured_rank = self._rank_value(permission.get("min_rank", 0))
        required_rank = (
            self.DEFAULT_WITHDRAW_RANK
            if configured_rank == 0
            else configured_rank
        )
        if locked:
            required_rank = max(required_rank, 5)
        if member["rank"] < required_rank:
            code = "LOCKED_ITEM" if locked else "PERMISSION_DENIED"
            raise VaultError(
                code,
                {
                    "guild_id": guild_id,
                    "character_id": member["character_id"],
                    "slot": slot,
                    "rank": member["rank"],
                    "required_rank": required_rank,
                    "locked": locked,
                },
            )

    def _daily_limit(self, guild_id, rank):
        limits = self._permissions.get(guild_id, {}).get("daily_limits", {})
        if rank in limits:
            return limits[rank]
        return self.default_daily_limit

    @staticmethod
    def _account_id(member):
        account_id = member.get("account_id")
        if account_id is None or account_id == "":
            return member["character_id"]
        return account_id

    @staticmethod
    def _day(now):
        if now is None:
            now = 0
        return int(now) // GuildVaultPermission.DAY_SECONDS

    def _record_log(
        self,
        guild_id,
        action,
        character_id,
        account_id,
        slot,
        item_id,
        quantity,
        bound,
        now,
        before_quantity=None,
        after_quantity=None,
    ):
        timestamp = self._normalise_timestamp(now)
        entry = {
            "action": action,
            "guild_id": guild_id,
            "character_id": character_id,
            "account_id": account_id,
            "slot": slot,
            "item_id": item_id,
            "quantity": quantity,
            "bound": bool(bound),
            "before_quantity": before_quantity,
            "after_quantity": after_quantity,
            "timestamp": timestamp,
            "time": timestamp,
            "now": timestamp,
        }
        self._logs.setdefault(guild_id, []).append(entry)

    @staticmethod
    def _normalise_timestamp(now):
        if now is None:
            return 0
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            return now
        return int(time.time())

    def _outstanding_for(self, guild_id, character_id):
        totals = {}
        details = {}
        for entry in self._logs.get(guild_id, []):
            if entry.get("character_id") != character_id:
                continue
            action = entry.get("action")
            if action not in ("withdraw", "deposit"):
                continue
            item_id = entry.get("item_id")
            bound = bool(entry.get("bound", False))
            key = (item_id, bound)
            current = totals.get(key, 0)
            if action == "withdraw":
                current += entry.get("quantity", 0)
            else:
                current -= entry.get("quantity", 0)
            totals[key] = current
            detail = details.setdefault(
                key, {"slots": set(), "last_timestamp": 0}
            )
            if entry.get("slot") is not None:
                detail["slots"].add(entry["slot"])
            detail["last_timestamp"] = max(
                detail["last_timestamp"],
                self._normalise_timestamp(entry.get("timestamp", 0)),
            )

        outstanding = []
        for (item_id, bound), quantity in sorted(totals.items()):
            if quantity > 0:
                detail = details[(item_id, bound)]
                slots = sorted(detail["slots"])
                outstanding.append(
                    {
                        "character_id": character_id,
                        "item_id": item_id,
                        "quantity": quantity,
                        "bound": bound,
                        "slot": slots[0] if len(slots) == 1 else None,
                        "slots": slots,
                        "timestamp": detail["last_timestamp"],
                    }
                )
        return outstanding

    def _slot_copy(self, guild_id, slot):
        stack = self._inventory.get(guild_id, {}).get(slot)
        if stack is None:
            return None
        permission = self._permissions.get(guild_id, {}).get("slots", {}).get(
            slot, {}
        )
        return {
            "slot": slot,
            "item_id": stack["item_id"],
            "quantity": stack["quantity"],
            "bound": bool(stack["bound"]),
            "locked": bool(permission.get("locked", False)),
            "min_rank": self._rank_value(permission.get("min_rank", 0)),
        }

    @staticmethod
    def _empty_slot_copy(slot):
        return {
            "slot": slot,
            "item_id": None,
            "quantity": 0,
            "bound": False,
            "locked": False,
            "min_rank": GuildVaultPermission.DEFAULT_WITHDRAW_RANK,
        }

    @staticmethod
    def _rank_value(rank):
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
            raise VaultError("INVALID_RANK", {"rank": rank})
        return rank

    @staticmethod
    def _slot_value(slot):
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise VaultError("INVALID_SLOT", {"slot": slot})
        return slot

    @staticmethod
    def _positive_quantity(quantity):
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
        ):
            raise VaultError("INVALID_QUANTITY", {"quantity": quantity})
        return quantity

    @staticmethod
    def _non_negative_quantity(quantity):
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise VaultError("INVALID_QUANTITY", {"quantity": quantity})
        return quantity
