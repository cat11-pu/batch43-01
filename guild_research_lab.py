import json
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone


class ResearchError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _idle_slot():
    return {
        "tech_id": None,
        "started_at": None,
        "ends_at": None,
        "duration": None,
        "materials": 0,
    }


def _number(value, code, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchError(code, value)
    if value < minimum:
        raise ResearchError(code, value)
    return value


def _seconds(value, code):
    if isinstance(value, timedelta):
        value = value.total_seconds()
    return _number(value, code)


def _timestamp(value, code):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    return _number(value, code, minimum=float("-inf"))


def _prereq_list(prereq):
    if prereq is None:
        return []
    if isinstance(prereq, str):
        return [(prereq, 1)]
    if isinstance(prereq, dict):
        return [(tech, int(level)) for tech, level in prereq.items()]
    if (isinstance(prereq, (tuple, list)) and len(prereq) == 2
            and isinstance(prereq[0], str)
            and isinstance(prereq[1], (int, float))
            and not isinstance(prereq[1], bool)):
        return [(prereq[0], int(prereq[1]))]
    result = []
    for item in prereq:
        if isinstance(item, str):
            result.append((item, 1))
        else:
            tech, level = item
            result.append((tech, int(level)))
    return result


class GuildResearchLab:
    """Guild research slot, donations and atomic completion."""

    def __init__(self, wallet=None):
        self.wallet = wallet
        self._lock = threading.RLock()
        self._requirements = {}
        self._guilds = {}
        self._transactions = {}

    def set_level(self, guild_id, level):
        level = _number(level, "INVALID_GUILD_LEVEL")
        with self._lock:
            guild = self._guilds.get(guild_id)
            if guild is None or guild.get("dissolved"):
                guild = {
                    "level": level,
                    "tech_levels": {},
                    "donations": {},
                    "slot": None,
                    "dissolved": False,
                }
                self._guilds[guild_id] = guild
            else:
                guild["level"] = level
            return level

    def set_requirements(self, tech_id, required_points, prereq=None,
                         min_guild_level=1, duration=0, materials=0,
                         max_level=5):
        required_points = _number(
            required_points, "INVALID_REQUIRED_POINTS")
        min_guild_level = _number(
            min_guild_level, "INVALID_MIN_GUILD_LEVEL")
        duration = _seconds(duration, "INVALID_DURATION")
        materials = _number(materials, "INVALID_MATERIALS")
        max_level = _number(max_level, "INVALID_MAX_LEVEL")
        if isinstance(max_level, float):
            if not max_level.is_integer():
                raise ResearchError("INVALID_MAX_LEVEL", max_level)
            max_level = int(max_level)

        requirement = {
            "required_points": required_points,
            "prereq": deepcopy(prereq),
            "min_guild_level": min_guild_level,
            "duration": duration,
            "materials": materials,
            "max_level": max_level,
        }
        with self._lock:
            self._requirements[tech_id] = requirement
            for guild in self._guilds.values():
                if tech_id in guild["donations"]:
                    guild["donations"][tech_id] = min(
                        guild["donations"][tech_id], required_points)
            return deepcopy(requirement)

    def donate(self, guild_id, tech_id, points, txn_id=None):
        points = _number(points, "INVALID_POINTS")
        if points <= 0:
            raise ResearchError("INVALID_POINTS", points)

        with self._lock:
            if txn_id is not None:
                try:
                    seen = txn_id in self._transactions
                except TypeError:
                    raise ResearchError("INVALID_TXN_ID", txn_id)
                if seen:
                    total = self._donation_total(guild_id, tech_id)
                    requirement = self._requirements.get(tech_id)
                    remaining = (
                        max(0, requirement["required_points"] - total)
                        if requirement else 0
                    )
                    return {
                        "accepted": 0,
                        "returned": 0,
                        "total": total,
                        "remaining": remaining,
                        "duplicate": True,
                    }

            guild = self._require_guild(guild_id)
            requirement = self._require_tech(tech_id)
            level = guild["tech_levels"].get(tech_id, 0)
            if level >= requirement["max_level"]:
                raise ResearchError("TECH_MAXED", tech_id)

            total = guild["donations"].get(tech_id, 0)
            remaining = max(0, requirement["required_points"] - total)
            accepted = min(points, remaining)
            returned = points - accepted

            if accepted:
                self._charge(
                    accepted, "INSUFFICIENT_FUNDS", "donation")
                total += accepted
                guild["donations"][tech_id] = total

            if txn_id is not None:
                self._transactions[txn_id] = {
                    "txn_id": txn_id,
                    "guild_id": guild_id,
                    "tech_id": tech_id,
                    "requested": points,
                    "accepted": accepted,
                    "returned": returned,
                }

            return {
                "accepted": accepted,
                "returned": returned,
                "total": total,
                "remaining": max(
                    0, requirement["required_points"] - total),
                "duplicate": False,
            }

    def start_research(self, guild_id, tech_id, now=0):
        now = _timestamp(now, "INVALID_TIME")
        with self._lock:
            guild = self._require_guild(guild_id)
            requirement = self._require_tech(tech_id)

            if guild["slot"] is not None:
                raise ResearchError("SLOT_BUSY", guild["slot"]["tech_id"])

            level = guild["tech_levels"].get(tech_id, 0)
            if level >= requirement["max_level"]:
                raise ResearchError("TECH_MAXED", tech_id)

            self._check_prerequisites(guild, requirement)

            if guild["level"] < requirement["min_guild_level"]:
                raise ResearchError(
                    "GUILD_LEVEL_TOO_LOW", guild["level"])

            total = guild["donations"].get(tech_id, 0)
            if total < requirement["required_points"]:
                raise ResearchError(
                    "NOT_ENOUGH_POINTS", total)

            materials = requirement["materials"]
            if materials:
                self._charge(
                    materials, "NOT_ENOUGH_MATERIALS", "materials")

            slot = {
                "tech_id": tech_id,
                "started_at": now,
                "ends_at": now + requirement["duration"],
                "duration": requirement["duration"],
                "materials": materials,
                "required_points": requirement["required_points"],
                "min_guild_level": requirement["min_guild_level"],
                "max_level": requirement["max_level"],
            }
            guild["slot"] = slot
            return deepcopy(slot)

    def finish_research(self, guild_id, tech_id, now=0):
        now = _timestamp(now, "INVALID_TIME")
        with self._lock:
            guild = self._guilds.get(guild_id)
            if guild is None:
                raise ResearchError("GUILD_NOT_FOUND", guild_id)
            if guild.get("dissolved"):
                raise ResearchError("GUILD_DISSOLVED", guild_id)

            slot = guild["slot"]
            if slot is None:
                raise ResearchError("NO_ACTIVE_RESEARCH", tech_id)
            if slot["tech_id"] != tech_id:
                raise ResearchError("RESEARCH_NOT_FOUND", tech_id)
            if now < slot["ends_at"]:
                raise ResearchError(
                    "NOT_COMPLETE", slot["ends_at"])

            requirement = self._requirements.get(tech_id)
            min_guild_level = (
                requirement["min_guild_level"]
                if requirement is not None
                else slot.get("min_guild_level", 0)
            )
            if guild["level"] < min_guild_level:
                raise ResearchError(
                    "GUILD_LEVEL_TOO_LOW", guild["level"])

            level = guild["tech_levels"].get(tech_id, 0)
            max_level = (
                requirement["max_level"]
                if requirement is not None
                else slot.get("max_level", level + 1)
            )
            if level >= max_level:
                raise ResearchError("TECH_MAXED", tech_id)

            level += 1
            guild["tech_levels"][tech_id] = level
            guild["donations"][tech_id] = 0
            guild["slot"] = None
            return {
                "granted": True,
                "tech_id": tech_id,
                "level": level,
            }

    def cancel_research(self, guild_id, tech_id, now=0):
        now = _timestamp(now, "INVALID_TIME")
        with self._lock:
            guild = self._require_guild(guild_id)
            slot = guild["slot"]
            if slot is None:
                raise ResearchError("NO_ACTIVE_RESEARCH", tech_id)
            if slot["tech_id"] != tech_id:
                raise ResearchError("RESEARCH_NOT_FOUND", tech_id)

            started_at = slot["started_at"]
            ends_at = slot["ends_at"]
            duration = ends_at - started_at
            materials = slot.get("materials", 0)
            if duration <= 0:
                progress = 1 if now >= ends_at else 0
            else:
                elapsed = min(max(now - started_at, 0), duration)
                progress = elapsed / duration
            if isinstance(materials, int) and isinstance(elapsed, int) \
                    and isinstance(duration, int):
                refunded = min(materials, materials * elapsed // duration)
            else:
                refunded = min(
                    materials, int(materials * progress))

            if refunded:
                self._credit(refunded, "refund")

            guild["slot"] = None
            return {
                "cancelled": True,
                "tech_id": tech_id,
                "refunded": refunded,
                "progress": progress,
            }

    def research_slot(self, guild_id):
        with self._lock:
            guild = self._guilds.get(guild_id)
            if (guild is None or guild.get("dissolved")
                    or guild["slot"] is None):
                return _idle_slot()
            slot = deepcopy(guild["slot"])
            return {
                "tech_id": slot["tech_id"],
                "started_at": slot["started_at"],
                "ends_at": slot["ends_at"],
                "duration": slot["duration"],
                "materials": slot["materials"],
            }

    def tech_level(self, guild_id, tech_id):
        with self._lock:
            guild = self._guilds.get(guild_id)
            if guild is None or guild.get("dissolved"):
                return 0
            return guild["tech_levels"].get(tech_id, 0)

    def disband(self, guild_id):
        with self._lock:
            guild = self._guilds.get(guild_id)
            if guild is None:
                guild = {
                    "level": 0,
                    "tech_levels": {},
                    "donations": {},
                    "slot": None,
                    "dissolved": False,
                }
                self._guilds[guild_id] = guild
            guild["dissolved"] = True
            guild["slot"] = None
            guild["tech_levels"] = {}
            guild["donations"] = {}
            guild["level"] = 0
            return True

    def dump(self):
        with self._lock:
            state = {
                "requirements": deepcopy(self._requirements),
                "guilds": deepcopy(self._guilds),
                "transactions": [
                    deepcopy(record)
                    for record in self._transactions.values()
                ],
            }
        return json.loads(json.dumps(state))

    def load(self, state):
        if isinstance(state, (str, bytes, bytearray)):
            state = json.loads(state)
        if not isinstance(state, dict):
            raise ResearchError("INVALID_STATE", state)

        requirements = {}
        for tech_id, raw in (state.get("requirements") or {}).items():
            if not isinstance(raw, dict):
                continue
            requirements[tech_id] = {
                "required_points": _number(
                    raw.get("required_points", 0),
                    "INVALID_STATE"),
                "prereq": deepcopy(raw.get("prereq")),
                "min_guild_level": _number(
                    raw.get("min_guild_level", 1),
                    "INVALID_STATE"),
                "duration": _seconds(
                    raw.get("duration", 0), "INVALID_STATE"),
                "materials": _number(
                    raw.get("materials", 0), "INVALID_STATE"),
                "max_level": int(raw.get("max_level", 5)),
            }

        guilds = {}
        for guild_id, raw in (state.get("guilds") or {}).items():
            if not isinstance(raw, dict):
                continue
            tech_levels = {
                tech_id: max(0, int(level))
                for tech_id, level
                in (raw.get("tech_levels") or {}).items()
            }
            donations = {}
            for tech_id, total in (raw.get("donations") or {}).items():
                requirement = requirements.get(tech_id)
                cap = (
                    requirement["required_points"]
                    if requirement is not None else max(0, int(total))
                )
                donations[tech_id] = min(max(0, total), cap)

            slot = raw.get("slot")
            if not isinstance(slot, dict) or not slot.get("tech_id"):
                slot = None
            else:
                started_at = _timestamp(
                    slot.get("started_at", 0), "INVALID_STATE")
                ends_at = _timestamp(
                    slot.get("ends_at", started_at), "INVALID_STATE")
                slot = {
                    "tech_id": slot["tech_id"],
                    "started_at": started_at,
                    "ends_at": ends_at,
                    "duration": ends_at - started_at,
                    "materials": max(
                        0, _number(slot.get("materials", 0),
                                   "INVALID_STATE")),
                    "required_points": max(
                        0, _number(slot.get("required_points", 0),
                                   "INVALID_STATE")),
                    "min_guild_level": max(
                        0, _number(slot.get("min_guild_level", 0),
                                   "INVALID_STATE")),
                    "max_level": max(
                        0, _number(slot.get("max_level", 0),
                                   "INVALID_STATE")),
                }

            dissolved = bool(raw.get("dissolved", False))
            guilds[guild_id] = {
                "level": max(
                    0, _number(raw.get("level", 0), "INVALID_STATE")),
                "tech_levels": tech_levels,
                "donations": donations,
                "slot": None if dissolved else slot,
                "dissolved": dissolved,
            }

        transactions = {}
        raw_transactions = state.get("transactions") or {}
        if isinstance(raw_transactions, dict):
            records = []
            for txn_id, record in raw_transactions.items():
                record = deepcopy(record)
                record.setdefault("txn_id", txn_id)
                records.append(record)
        else:
            records = raw_transactions
        for record in records:
            if isinstance(record, dict) and "txn_id" in record:
                transactions[record["txn_id"]] = deepcopy(record)

        with self._lock:
            self._requirements = requirements
            self._guilds = guilds
            self._transactions = transactions
        return True

    def _donation_total(self, guild_id, tech_id):
        guild = self._guilds.get(guild_id)
        if guild is None or guild.get("dissolved"):
            return 0
        return guild["donations"].get(tech_id, 0)

    def _require_guild(self, guild_id):
        guild = self._guilds.get(guild_id)
        if guild is None:
            raise ResearchError("GUILD_NOT_FOUND", guild_id)
        if guild.get("dissolved"):
            raise ResearchError("GUILD_DISSOLVED", guild_id)
        return guild

    def _require_tech(self, tech_id):
        requirement = self._requirements.get(tech_id)
        if requirement is None:
            raise ResearchError("TECH_NOT_FOUND", tech_id)
        return requirement

    def _check_prerequisites(self, guild, requirement):
        for prereq, required_level in _prereq_list(
                requirement.get("prereq")):
            if guild["tech_levels"].get(prereq, 0) < required_level:
                raise ResearchError("PREREQ_MISSING", prereq)

    def _wallet_balance(self):
        wallet = self.wallet
        if wallet is None:
            return None
        if isinstance(wallet, dict):
            for key in ("balance", "amount", "points", "credits", "gold"):
                if key in wallet:
                    return _number(wallet[key], "WALLET_ERROR")
            return None
        if isinstance(wallet, (int, float)) and not isinstance(wallet, bool):
            return wallet
        for name in ("balance", "amount", "points", "credits", "gold"):
            try:
                value = getattr(wallet, name)
            except (AttributeError, TypeError):
                continue
            if callable(value):
                try:
                    value = value()
                except TypeError:
                    continue
            if isinstance(value, (int, float)) and not isinstance(
                    value, bool):
                return value
        for name in ("get_balance", "balance"):
            method = getattr(wallet, name, None)
            if callable(method):
                try:
                    value = method()
                except TypeError:
                    continue
                if isinstance(value, (int, float)) and not isinstance(
                        value, bool):
                    return value
        return None

    def _wallet_call(self, names, amount):
        wallet = self.wallet
        if wallet is None:
            return True
        for name in names:
            method = getattr(wallet, name, None)
            if not callable(method):
                continue
            try:
                result = method(amount)
            except TypeError:
                try:
                    result = method(amount=amount)
                except TypeError:
                    continue
            return result is not False
        return False

    def _charge(self, amount, code, detail):
        if not amount or self.wallet is None:
            return
        balance = self._wallet_balance()
        if balance is not None and balance < amount:
            raise ResearchError(code, detail)

        names = (
            "spend", "charge", "debit", "deduct", "pay",
            "withdraw", "take",
        )
        try:
            if self._wallet_call(names, amount):
                return
        except ResearchError:
            raise
        except Exception as exc:
            raise ResearchError(code, detail) from exc

        wallet = self.wallet
        if isinstance(wallet, (int, float)) and not isinstance(wallet, bool):
            if wallet < amount:
                raise ResearchError(code, detail)
            self.wallet = wallet - amount
            return
        if isinstance(wallet, dict):
            for key in ("balance", "amount", "points", "credits", "gold"):
                if key in wallet:
                    if wallet[key] < amount:
                        raise ResearchError(code, detail)
                    wallet[key] -= amount
                    return
        if balance is not None:
            if balance < amount:
                raise ResearchError(code, detail)
            try:
                setattr(wallet, "balance", balance - amount)
                return
            except (AttributeError, TypeError):
                pass
        raise ResearchError("WALLET_ERROR", detail)

    def _credit(self, amount, detail):
        if not amount or self.wallet is None:
            return
        names = (
            "refund", "credit", "add", "receive", "gain", "deposit",
        )
        try:
            if self._wallet_call(names, amount):
                return
        except ResearchError:
            raise
        except Exception as exc:
            raise ResearchError("WALLET_ERROR", detail) from exc

        wallet = self.wallet
        if isinstance(wallet, (int, float)) and not isinstance(wallet, bool):
            self.wallet = wallet + amount
            return
        if isinstance(wallet, dict):
            for key in ("balance", "amount", "points", "credits", "gold"):
                if key in wallet:
                    wallet[key] += amount
                    return
        balance = self._wallet_balance()
        if balance is not None:
            try:
                setattr(wallet, "balance", balance + amount)
                return
            except (AttributeError, TypeError):
                pass
        raise ResearchError("WALLET_ERROR", detail)
