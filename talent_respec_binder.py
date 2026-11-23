from __future__ import annotations

import copy
import threading


class TalentError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TalentRespecBinder:
    """Talent point ledger, scheme switching and respec fees."""

    SCHEME_VERSION = 2
    STATE_VERSION = 2

    def __init__(self, cost_per_level=100, switch_cooldown=10, wallet=None):
        self.cost_per_level = self._non_negative_int(
            cost_per_level, "INVALID_COST"
        )
        if isinstance(switch_cooldown, bool):
            raise TalentError("INVALID_COOLDOWN")
        try:
            switch_cooldown = int(switch_cooldown)
        except (TypeError, ValueError):
            raise TalentError("INVALID_COOLDOWN") from None
        if switch_cooldown < 0:
            raise TalentError("INVALID_COOLDOWN")

        self.switch_cooldown = switch_cooldown
        self.wallet = wallet
        self._lock = threading.RLock()
        self._characters = {}

    @staticmethod
    def _non_negative_int(value, code):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TalentError(code)
        if value < 0:
            raise TalentError(code)
        return value

    @staticmethod
    def _positive_int(value, code):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TalentError(code)
        if value <= 0:
            raise TalentError(code)
        return value

    @classmethod
    def _character_key(cls, character):
        if isinstance(character, dict):
            for field in ("character_id", "id", "name"):
                if field in character:
                    character = character[field]
                    break

        if isinstance(character, bool) or character is None:
            raise TalentError("INVALID_CHARACTER")
        if not isinstance(character, str):
            character = str(character)
        if not character:
            raise TalentError("INVALID_CHARACTER")
        return character

    @staticmethod
    def _new_character():
        return {
            "earned": 0,
            "equipment": {},
            "items": [],
            "level": 0,
            "max_level": 0,
            "schemes": {},
            "active_scheme": None,
            "allocation": {},
            "last_switch": None,
            "cooldowns": {},
            "scheme_cooldowns": {},
            "mailbox": [],
        }

    def _get(self, character):
        key = self._character_key(character)
        state = self._characters.get(key)
        if state is None:
            state = self._new_character()
            self._characters[key] = state
        return key, state

    @staticmethod
    def _equipment_total(state):
        return sum(int(points) for points in state["equipment"].values())

    @classmethod
    def _total(cls, state):
        return int(state["earned"]) + cls._equipment_total(state)

    @staticmethod
    def _allocation_total(allocation):
        return sum(int(points) for points in allocation.values())

    @classmethod
    def _normalise_allocation(cls, allocation):
        if allocation is None or isinstance(allocation, (str, bytes)):
            raise TalentError("INVALID_ALLOCATION")
        if not hasattr(allocation, "items"):
            raise TalentError("INVALID_ALLOCATION")

        result = {}
        for node, points in allocation.items():
            if not isinstance(node, str) or not node:
                raise TalentError("INVALID_ALLOCATION")
            if isinstance(points, bool) or not isinstance(points, int):
                raise TalentError("INVALID_POINTS")
            if points < 0:
                raise TalentError("INVALID_POINTS")
            if points:
                result[node] = points
        return result

    @classmethod
    def _validate_allocation(cls, allocation, total):
        result = cls._normalise_allocation(allocation)
        if cls._allocation_total(result) > total:
            raise TalentError("INSUFFICIENT_POINTS")
        return result

    @staticmethod
    def _passives_for(allocation):
        return [
            node
            for node, points in allocation.items()
            if points > 0 and "passive" in node.lower()
        ]

    @staticmethod
    def _stats_for(allocation):
        return {
            node: points
            for node, points in allocation.items()
            if points > 0 and "passive" not in node.lower()
        }

    @classmethod
    def _trim_to_total(cls, state, total):
        """Remove allocations from the end until every build fits the budget."""

        def trim(allocation):
            allocation = dict(allocation)
            excess = cls._allocation_total(allocation) - total
            for node in reversed(list(allocation)):
                if excess <= 0:
                    break
                removed = min(excess, allocation[node])
                allocation[node] -= removed
                excess -= removed
                if allocation[node] == 0:
                    del allocation[node]
            return allocation

        state["allocation"] = trim(state["allocation"])
        for scheme in state["schemes"].values():
            scheme["allocation"] = trim(scheme["allocation"])

    @classmethod
    def _snapshot_state(cls, key, state):
        total = cls._total(state)
        allocation = copy.deepcopy(state["allocation"])
        spent = cls._allocation_total(allocation)
        schemes = {
            scheme_id: {
                "scheme_id": scheme_id,
                "version": cls.SCHEME_VERSION,
                "revision": scheme["revision"],
                "allocation": copy.deepcopy(scheme["allocation"]),
            }
            for scheme_id, scheme in state["schemes"].items()
        }
        return {
            "character": key,
            "total": total,
            "spent": spent,
            "remaining": total - spent,
            "level": state["level"],
            "max_level": state["max_level"],
            "active_scheme": state["active_scheme"],
            "allocation": allocation,
            "nodes": copy.deepcopy(allocation),
            "passives": cls._passives_for(allocation),
            "stats": cls._stats_for(allocation),
            "items": copy.deepcopy(state["items"]),
            "equipped_bonus": copy.deepcopy(state["equipment"]),
            "equipment_bonus": copy.deepcopy(state["equipment"]),
            "bonus_points": cls._equipment_total(state),
            "cooldowns": copy.deepcopy(state["cooldowns"]),
            "scheme_cooldowns": copy.deepcopy(state["scheme_cooldowns"]),
            "last_switch": state["last_switch"],
            "schemes": schemes,
            "revisions": {
                scheme_id: scheme["revision"]
                for scheme_id, scheme in state["schemes"].items()
            },
            "mailbox": copy.deepcopy(state["mailbox"]),
            "mail": copy.deepcopy(state["mailbox"]),
        }

    def _charge_wallet(self, amount):
        if self.wallet is None:
            return None

        wallet = self.wallet
        try:
            if callable(wallet):
                result = wallet(amount)
            else:
                result = None
                for name in ("charge", "withdraw", "debit", "spend"):
                    method = getattr(wallet, name, None)
                    if callable(method):
                        result = method(amount)
                        break
                else:
                    raise TalentError("WALLET_UNAVAILABLE")
        except TalentError:
            raise
        except Exception as exc:
            raise TalentError("RESPEC_ROLLBACK", str(exc)) from exc

        if result is False:
            raise TalentError("RESPEC_ROLLBACK")
        if isinstance(result, dict) and result.get("ok") is False:
            raise TalentError("RESPEC_ROLLBACK", result)
        return result

    def _refund_wallet(self, amount):
        if self.wallet is None:
            return
        wallet = self.wallet
        try:
            for name in ("refund", "credit", "deposit"):
                method = getattr(wallet, name, None)
                if callable(method):
                    method(amount)
                    return
        except Exception:
            pass

    def grant_points(self, character, points, source="level", item=None):
        points = self._positive_int(points, "INVALID_POINTS")
        if not isinstance(source, str) or not source:
            raise TalentError("INVALID_SOURCE")
        if item is not None and (
            not isinstance(item, str) or not item
        ):
            raise TalentError("INVALID_ITEM")

        with self._lock:
            key, state = self._get(character)
            state["earned"] += points
            if item is not None:
                state["items"].append(item)
            return self._snapshot_state(key, state)

    def set_level(self, character, level):
        level = self._non_negative_int(level, "INVALID_LEVEL")
        with self._lock:
            key, state = self._get(character)
            state["level"] = level
            state["max_level"] = max(state["max_level"], level)
            return self._snapshot_state(key, state)

    def spend_point(self, character, node, points=1):
        points = self._positive_int(points, "INVALID_POINTS")
        if not isinstance(node, str) or not node:
            raise TalentError("INVALID_NODE")

        with self._lock:
            key, state = self._get(character)
            total = self._total(state)
            spent = self._allocation_total(state["allocation"])
            if spent + points > total:
                raise TalentError("INSUFFICIENT_POINTS")

            allocation = dict(state["allocation"])
            allocation[node] = allocation.get(node, 0) + points
            state["allocation"] = allocation

            active = state["active_scheme"]
            if active is not None:
                scheme = state["schemes"][active]
                scheme_allocation = dict(scheme["allocation"])
                scheme_allocation[node] = (
                    scheme_allocation.get(node, 0) + points
                )
                scheme["allocation"] = scheme_allocation
            return self._snapshot_state(key, state)

    def respec(self, character, level=None):
        if level is not None:
            level = self._non_negative_int(level, "INVALID_LEVEL")

        with self._lock:
            key, state = self._get(character)
            refunded = self._allocation_total(state["allocation"])
            items_returned = list(state["items"])
            effective_level = max(state["max_level"], level or 0)
            cost = effective_level * self.cost_per_level

            charged = False
            try:
                self._charge_wallet(cost)
                charged = self.wallet is not None
                staged = copy.deepcopy(state)
                staged["allocation"] = {}
                staged["active_scheme"] = None
                staged["last_switch"] = None
                staged["cooldowns"] = {}
                staged["scheme_cooldowns"] = {}
                staged["items"] = []
                mail_start = len(staged["mailbox"])
                for item in items_returned:
                    staged["mailbox"].append(
                        {
                            "character": key,
                            "item": item,
                            "reason": "respec",
                            "cost": cost,
                        }
                    )
                staged["mailbox"].append(
                    {
                        "character": key,
                        "reason": "respec_compensation",
                        "items": list(items_returned),
                        "points": refunded,
                        "cost": cost,
                    }
                )

                total = self._total(staged)
                if (
                    self._allocation_total(staged["allocation"]) > total
                    or total < 0
                ):
                    raise TalentError("RESPEC_ROLLBACK")
                self._characters[key] = staged
            except TalentError:
                if charged:
                    self._refund_wallet(cost)
                raise
            except Exception as exc:
                if charged:
                    self._refund_wallet(cost)
                raise TalentError("RESPEC_ROLLBACK", str(exc)) from exc

            mail = copy.deepcopy(staged["mailbox"][mail_start:])
            return {
                "character": key,
                "cost": cost,
                "level": effective_level,
                "refunded": refunded,
                "items_returned": items_returned,
                "items": items_returned,
                "remaining": self._total(staged),
                "total": self._total(staged),
                "compensation": {
                    "items": items_returned,
                    "points": refunded,
                    "cost": cost,
                },
                "mail": mail,
            }

    def save_scheme(self, character, scheme_id, allocation, revision,
                    base_revision=0):
        if not isinstance(scheme_id, str) or not scheme_id:
            raise TalentError("INVALID_SCHEME")
        revision = self._non_negative_int(revision, "INVALID_REVISION")
        base_revision = self._non_negative_int(
            base_revision, "INVALID_REVISION"
        )

        with self._lock:
            key, state = self._get(character)
            current = state["schemes"].get(scheme_id)
            current_revision = current["revision"] if current else 0
            if base_revision != current_revision:
                raise TalentError(
                    "REVISION_CONFLICT",
                    {
                        "expected": current_revision,
                        "actual": base_revision,
                    },
                )
            if revision <= current_revision:
                raise TalentError(
                    "REVISION_CONFLICT",
                    {
                        "expected": f">{current_revision}",
                        "actual": revision,
                    },
                )

            normalised = self._validate_allocation(
                allocation, self._total(state)
            )
            staged = copy.deepcopy(state)
            staged["schemes"][scheme_id] = {
                "scheme_id": scheme_id,
                "version": self.SCHEME_VERSION,
                "revision": revision,
                "allocation": normalised,
            }
            if staged["active_scheme"] == scheme_id:
                staged["allocation"] = copy.deepcopy(normalised)
                staged["cooldowns"] = {}
                staged["scheme_cooldowns"][scheme_id] = {}
            self._characters[key] = staged

            return {
                "scheme_id": scheme_id,
                "version": self.SCHEME_VERSION,
                "revision": revision,
                "allocation": copy.deepcopy(normalised),
                "active": staged["active_scheme"] == scheme_id,
            }

    def activate_scheme(self, character, scheme_id, now=0):
        if not isinstance(scheme_id, str) or not scheme_id:
            raise TalentError("INVALID_SCHEME")
        if isinstance(now, bool):
            raise TalentError("INVALID_TIME")
        try:
            now = float(now)
        except (TypeError, ValueError):
            raise TalentError("INVALID_TIME") from None
        if now != now or now in (float("inf"), float("-inf")):
            raise TalentError("INVALID_TIME")

        with self._lock:
            key, state = self._get(character)
            scheme = state["schemes"].get(scheme_id)
            if scheme is None:
                raise TalentError("SCHEME_NOT_FOUND")

            previous = state["active_scheme"]
            if previous == scheme_id:
                return self._snapshot_state(key, state)

            if (
                previous is not None
                and state["last_switch"] is not None
                and now - state["last_switch"] < self.switch_cooldown
            ):
                raise TalentError(
                    "SWITCH_COOLDOWN",
                    {
                        "remaining": (
                            self.switch_cooldown
                            - (now - state["last_switch"])
                        )
                    },
                )

            allocation = self._validate_allocation(
                scheme["allocation"], self._total(state)
            )
            staged = copy.deepcopy(state)
            staged["allocation"] = allocation
            staged["active_scheme"] = scheme_id
            staged["last_switch"] = now
            staged["cooldowns"] = {}
            staged["scheme_cooldowns"][scheme_id] = {}
            self._characters[key] = staged

            return {
                "character": key,
                "previous_scheme": previous,
                "active_scheme": scheme_id,
                "allocation": copy.deepcopy(allocation),
                "passives": self._passives_for(allocation),
                "stats": self._stats_for(allocation),
                "spent": self._allocation_total(allocation),
                "remaining": self._total(staged)
                - self._allocation_total(allocation),
                "cooldowns_reset": True,
                "cooldowns": {},
            }

    def import_scheme(self, character, scheme_id, payload):
        if not isinstance(payload, dict):
            raise TalentError("SCHEME_VERSION_MISMATCH")

        version = payload.get("version", payload.get("scheme_version"))
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise TalentError("SCHEME_VERSION_MISMATCH") from None
        if version != self.SCHEME_VERSION:
            raise TalentError(
                "SCHEME_VERSION_MISMATCH",
                {"expected": self.SCHEME_VERSION, "actual": version},
            )

        allocation = payload.get("allocation")
        if allocation is None:
            allocation = payload.get("nodes")
        if allocation is None:
            allocation = payload.get("talents")
        if allocation is None:
            raise TalentError("INVALID_SCHEME")

        with self._lock:
            key, state = self._get(character)
            current = state["schemes"].get(scheme_id)
            current_revision = current["revision"] if current else 0

            base_revision = payload.get("base_revision")
            if base_revision is not None:
                base_revision = self._non_negative_int(
                    base_revision, "INVALID_REVISION"
                )
                if base_revision != current_revision:
                    raise TalentError("REVISION_CONFLICT")

            revision = payload.get("revision")
            if revision is None:
                revision = current_revision + 1
            else:
                revision = self._non_negative_int(
                    revision, "INVALID_REVISION"
                )
                if revision <= current_revision:
                    raise TalentError("REVISION_CONFLICT")

            normalised = self._validate_allocation(
                allocation, self._total(state)
            )
            staged = copy.deepcopy(state)
            staged["schemes"][scheme_id] = {
                "scheme_id": scheme_id,
                "version": self.SCHEME_VERSION,
                "revision": revision,
                "allocation": normalised,
            }
            if staged["active_scheme"] == scheme_id:
                staged["allocation"] = copy.deepcopy(normalised)
                staged["cooldowns"] = {}
                staged["scheme_cooldowns"][scheme_id] = {}
            self._characters[key] = staged

            return {
                "scheme_id": scheme_id,
                "version": self.SCHEME_VERSION,
                "revision": revision,
                "allocation": copy.deepcopy(normalised),
                "active": staged["active_scheme"] == scheme_id,
            }

    def equip_bonus(self, character, item_id, points):
        if not isinstance(item_id, str) or not item_id:
            raise TalentError("INVALID_ITEM")
        points = self._positive_int(points, "INVALID_POINTS")

        with self._lock:
            key, state = self._get(character)
            staged = copy.deepcopy(state)
            staged["equipment"][item_id] = points
            total = self._total(staged)
            self._trim_to_total(staged, total)
            self._characters[key] = staged
            return self._snapshot_state(key, staged)

    def unequip_bonus(self, character, item_id):
        if not isinstance(item_id, str) or not item_id:
            raise TalentError("INVALID_ITEM")

        with self._lock:
            key, state = self._get(character)
            if item_id not in state["equipment"]:
                raise TalentError("BONUS_NOT_FOUND")
            staged = copy.deepcopy(state)
            del staged["equipment"][item_id]
            total = self._total(staged)
            self._trim_to_total(staged, total)
            self._characters[key] = staged
            return self._snapshot_state(key, staged)

    def preview(self, character, scheme_id):
        if not isinstance(scheme_id, str) or not scheme_id:
            raise TalentError("INVALID_SCHEME")

        with self._lock:
            key, state = self._get(character)
            scheme = state["schemes"].get(scheme_id)
            if scheme is None:
                raise TalentError("SCHEME_NOT_FOUND")
            total = self._total(state)
            allocation = copy.deepcopy(scheme["allocation"])
            spent = self._allocation_total(allocation)
            return {
                "character": key,
                "scheme_id": scheme_id,
                "version": self.SCHEME_VERSION,
                "revision": scheme["revision"],
                "allocation": allocation,
                "nodes": copy.deepcopy(allocation),
                "passives": self._passives_for(allocation),
                "stats": self._stats_for(allocation),
                "total": total,
                "spent": spent,
                "remaining": total - spent,
                "active_scheme": state["active_scheme"],
                "cooldowns": copy.deepcopy(
                    state["scheme_cooldowns"].get(scheme_id, {})
                ),
            }

    def snapshot(self, character):
        with self._lock:
            key, state = self._get(character)
            return self._snapshot_state(key, state)

    def dump(self):
        with self._lock:
            characters = {}
            for key, state in self._characters.items():
                row = copy.deepcopy(state)
                for scheme in row["schemes"].values():
                    scheme["version"] = self.SCHEME_VERSION
                characters[key] = row
            return {
                "version": self.STATE_VERSION,
                "state_version": self.STATE_VERSION,
                "cost_per_level": self.cost_per_level,
                "switch_cooldown": self.switch_cooldown,
                "characters": characters,
            }

    def load(self, state):
        if not isinstance(state, dict):
            raise TalentError("INVALID_STATE")
        version = state.get("version", state.get("state_version"))
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise TalentError("STATE_VERSION_MISMATCH") from None
        if version != self.STATE_VERSION:
            raise TalentError(
                "STATE_VERSION_MISMATCH",
                {"expected": self.STATE_VERSION, "actual": version},
            )

        raw_characters = state.get("characters")
        if not isinstance(raw_characters, dict):
            raise TalentError("INVALID_STATE")

        characters = {}
        for key, raw in raw_characters.items():
            key = self._character_key(key)
            if not isinstance(raw, dict):
                raise TalentError("INVALID_STATE")
            row = self._new_character()

            equipment = raw.get("equipment", raw.get("equipped_bonus", {}))
            if not isinstance(equipment, dict):
                raise TalentError("INVALID_STATE")
            for item_id, points in equipment.items():
                if not isinstance(item_id, str) or not item_id:
                    raise TalentError("INVALID_STATE")
                row["equipment"][item_id] = self._non_negative_int(
                    points, "INVALID_STATE"
                )

            if "earned" in raw:
                row["earned"] = self._non_negative_int(
                    raw["earned"], "INVALID_STATE"
                )
            else:
                total = self._non_negative_int(
                    raw.get("total", 0), "INVALID_STATE"
                )
                row["earned"] = max(
                    0, total - self._equipment_total(row)
                )

            row["level"] = self._non_negative_int(
                raw.get("level", 0), "INVALID_STATE"
            )
            row["max_level"] = max(
                row["level"],
                self._non_negative_int(
                    raw.get("max_level", row["level"]), "INVALID_STATE"
                ),
            )
            row["allocation"] = self._normalise_allocation(
                raw.get("allocation", raw.get("nodes", {}))
            )

            items = raw.get("items", [])
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item for item in items
            ):
                raise TalentError("INVALID_STATE")
            row["items"] = list(items)

            schemes = raw.get("schemes", {})
            if not isinstance(schemes, dict):
                raise TalentError("INVALID_STATE")
            for scheme_id, scheme in schemes.items():
                if not isinstance(scheme_id, str) or not scheme_id:
                    raise TalentError("INVALID_STATE")
                if not isinstance(scheme, dict):
                    raise TalentError("INVALID_STATE")
                try:
                    scheme_version = int(
                        scheme.get("version", self.SCHEME_VERSION)
                    )
                except (TypeError, ValueError):
                    raise TalentError("SCHEME_VERSION_MISMATCH") from None
                if scheme_version != self.SCHEME_VERSION:
                    raise TalentError("SCHEME_VERSION_MISMATCH")
                revision = self._non_negative_int(
                    scheme.get("revision", 0), "INVALID_STATE"
                )
                allocation = self._normalise_allocation(
                    scheme.get("allocation", {})
                )
                row["schemes"][scheme_id] = {
                    "scheme_id": scheme_id,
                    "version": self.SCHEME_VERSION,
                    "revision": revision,
                    "allocation": allocation,
                }

            active = raw.get("active_scheme")
            if active is not None:
                if active not in row["schemes"]:
                    raise TalentError("INVALID_STATE")
                row["active_scheme"] = active
                row["allocation"] = copy.deepcopy(
                    row["schemes"][active]["allocation"]
                )

            last_switch = raw.get("last_switch")
            if last_switch is not None:
                if isinstance(last_switch, bool) or not isinstance(
                    last_switch, (int, float)
                ):
                    raise TalentError("INVALID_STATE")
                row["last_switch"] = last_switch

            cooldowns = raw.get("cooldowns", {})
            if not isinstance(cooldowns, dict):
                raise TalentError("INVALID_STATE")
            row["cooldowns"] = copy.deepcopy(cooldowns)

            scheme_cooldowns = raw.get("scheme_cooldowns", {})
            if not isinstance(scheme_cooldowns, dict):
                raise TalentError("INVALID_STATE")
            row["scheme_cooldowns"] = copy.deepcopy(scheme_cooldowns)

            mailbox = raw.get("mailbox", [])
            if not isinstance(mailbox, list):
                raise TalentError("INVALID_STATE")
            row["mailbox"] = copy.deepcopy(mailbox)

            total = self._total(row)
            spent = self._allocation_total(row["allocation"])
            if spent > total:
                raise TalentError("INVALID_STATE")
            for scheme in row["schemes"].values():
                if self._allocation_total(scheme["allocation"]) > total:
                    raise TalentError("INVALID_STATE")
            characters[key] = row

        cost_per_level = self._non_negative_int(
            state.get("cost_per_level", self.cost_per_level),
            "INVALID_STATE",
        )
        switch_cooldown = self._non_negative_int(
            state.get("switch_cooldown", self.switch_cooldown),
            "INVALID_STATE",
        )

        with self._lock:
            self.cost_per_level = cost_per_level
            self.switch_cooldown = switch_cooldown
            self._characters = characters
        return {
            "version": self.STATE_VERSION,
            "characters": len(characters),
        }
