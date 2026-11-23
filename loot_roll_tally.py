import copy
import threading


class LootError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class LootRollTally:
    """Per-drop need/greed roll state machine with delivery.

    A drop moves through ``open`` -> ``settled`` -> ``delivered``.  The same
    drop id is intentionally a singleton: once a round has been opened the
    stored state is reused, so replaying an old UI packet cannot start a
    second roll or duplicate a previously recorded vote.
    """

    _MODES = {"need_greed", "captain", "free_for_all"}

    def __init__(self, mailer=None):
        self.mailer = mailer
        self._rule = "need_greed"
        self._rolls = {}
        self._outbox = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Public state-machine API
    def open_roll(self, drop_id, item_id, mode=None, deadline=0,
                  eligible=(), now=0):
        with self._lock:
            if drop_id in self._rolls:
                # Reopening the same corpse/drop must show the original
                # record rather than appending a second set of votes.
                return copy.deepcopy(self._rolls[drop_id])

            selected_mode = self._rule if mode is None else mode
            if selected_mode not in self._MODES:
                raise LootError("INVALID_RULE", selected_mode)

            roster = self._normalize_eligible(eligible)
            state = {
                "drop_id": drop_id,
                "item_id": item_id,
                "mode": selected_mode,
                "deadline": deadline,
                "eligible": roster,
                "votes": [],
                "status": "open",
                "paused": False,
                "paused_at": None,
                "last_now": now,
                "winner": None,
                "tie": False,
                "tie_breaker": "character_id",
                "tie_break_key": "character_id",
                "tie_break": None,
                "settled_at": None,
                "delivered_to": None,
                "delivery_method": None,
                "deliveries": 0,
                "mailed": False,
                "looted_by": None,
            }
            self._rolls[drop_id] = state
            return copy.deepcopy(state)

    def vote(self, drop_id, character, roll, kind="need", bonus=0, now=0):
        with self._lock:
            state = self._get(drop_id)
            if state["status"] != "open":
                raise LootError("ROLL_CLOSED", drop_id)

            # The deadline and the settlement it triggers are one atomic
            # operation.  A vote arriving after the cutoff can never alter
            # the result.
            if not state["paused"] and self._expired(state, now):
                self._settle(state, now)
                raise LootError("ROLL_CLOSED", drop_id)

            character_id = self._character_id(character)
            roster_entry = self._roster_entry(state, character_id)
            if roster_entry is None and state["eligible"]:
                raise LootError("PLAYER_NOT_ELIGIBLE", character_id)
            if roster_entry is not None and roster_entry.get("departed"):
                raise LootError("PLAYER_NOT_ELIGIBLE", character_id)
            if roster_entry is not None and roster_entry.get("online") is False:
                raise LootError("PLAYER_OFFLINE", character_id)

            if isinstance(kind, str):
                kind = kind.lower()
            if kind not in ("need", "greed"):
                raise LootError("INVALID_VOTE_KIND", kind)
            if kind == "need" and roster_entry is not None and \
                    not roster_entry.get("can_need", True):
                raise LootError("CANNOT_NEED", character_id)

            for existing in state["votes"]:
                if existing["character"] == character_id:
                    raise LootError("ALREADY_VOTED", character_id)

            try:
                total = int(roll) + int(bonus)
            except (TypeError, ValueError):
                raise LootError("INVALID_ROLL", roll)

            state["votes"].append({
                "character": character_id,
                "roll": int(roll),
                "bonus": int(bonus),
                "total": total,
                "kind": kind,
                "at": now,
            })
            state["last_now"] = now
            return copy.deepcopy(state["votes"][-1])

    def close_roll(self, drop_id, now=0):
        with self._lock:
            state = self._get(drop_id)
            if state["paused"]:
                raise LootError("ROLL_PAUSED", drop_id)
            if state["status"] != "open":
                return copy.deepcopy(state)
            self._settle(state, now)
            return copy.deepcopy(state)

    def deliver(self, drop_id, character=None, free_slots=1):
        with self._lock:
            state = self._get(drop_id)

            if state["deliveries"]:
                return {
                    "delivered": False,
                    "mailed": state["mailed"],
                    "duplicate": True,
                    "winner": state["winner"],
                    "delivered_to": state["delivered_to"],
                }

            if state["looted_by"] is not None:
                return {
                    "delivered": False,
                    "mailed": False,
                    "duplicate": True,
                    "winner": state["winner"],
                    "delivered_to": state["looted_by"],
                }

            if state["mode"] == "free_for_all":
                return {
                    "delivered": False,
                    "mailed": False,
                    "reason": "FREE_FOR_ALL",
                    "winner": None,
                }

            if state["status"] == "open":
                self._settle(state, state["last_now"])

            recipient = state["winner"] if character is None else \
                self._character_id(character)
            if recipient is None:
                return {
                    "delivered": False,
                    "mailed": False,
                    "reason": "NO_WINNER",
                    "winner": None,
                }

            roster_entry = self._roster_entry(state, recipient)
            manual_delivery = character is not None
            if roster_entry is not None:
                if roster_entry.get("departed"):
                    if manual_delivery:
                        raise LootError("PLAYER_NOT_ELIGIBLE", recipient)
                    self._mail(state, recipient, "player_departed")
                    return {
                        "delivered": False,
                        "mailed": True,
                        "duplicate": False,
                        "winner": state["winner"],
                        "delivered_to": recipient,
                        "reason": "player_departed",
                    }
            elif state["eligible"]:
                # A leader cannot "rescue" a missing item by handing it to a
                # character who is not in the drop's eligible roster.
                raise LootError("PLAYER_NOT_ELIGIBLE", recipient)

            reason = None
            if state["eligible"] and roster_entry is not None and \
                    roster_entry.get("online") is False:
                reason = "player_offline"

            try:
                has_space = int(free_slots) > 0
            except (TypeError, ValueError):
                has_space = False

            if has_space and reason is None:
                state["deliveries"] = 1
                state["delivered_to"] = recipient
                state["delivery_method"] = "bag"
                state["status"] = "delivered"
                return {
                    "delivered": True,
                    "mailed": False,
                    "duplicate": False,
                    "winner": state["winner"],
                    "delivered_to": recipient,
                    "item_id": state["item_id"],
                }

            if reason is None:
                reason = "bag_full"
            self._mail(state, recipient, reason)
            return {
                "delivered": False,
                "mailed": True,
                "duplicate": False,
                "winner": state["winner"],
                "delivered_to": recipient,
                "reason": reason,
            }

    def assign(self, drop_id, character):
        with self._lock:
            state = self._get(drop_id)
            if state["deliveries"]:
                raise LootError("ALREADY_DELIVERED", drop_id)

            character_id = self._character_id(character)
            roster_entry = self._roster_entry(state, character_id)
            if roster_entry is None and state["eligible"]:
                raise LootError("PLAYER_NOT_ELIGIBLE", character_id)
            if roster_entry is not None and roster_entry.get("departed"):
                raise LootError("PLAYER_NOT_ELIGIBLE", character_id)
            if roster_entry is not None and roster_entry.get("online") is False:
                raise LootError("PLAYER_OFFLINE", character_id)

            if state["status"] == "settled" and state["winner"] not in (
                    None, character_id):
                raise LootError("ALREADY_SETTLED", drop_id)

            state["winner"] = character_id
            state["status"] = "settled"
            state["settled_at"] = state["last_now"]
            state["tie"] = False
            state["tie_break"] = None
            return copy.deepcopy(state)

    def pause(self, drop_id):
        with self._lock:
            state = self._get(drop_id)
            if state["status"] != "open":
                return copy.deepcopy(state)
            if not state["paused"]:
                state["paused"] = True
                state["paused_at"] = state["last_now"]
            return copy.deepcopy(state)

    def resume(self, drop_id, now=0):
        with self._lock:
            state = self._get(drop_id)
            if state["paused"]:
                paused_at = state["paused_at"]
                if paused_at is None:
                    paused_at = now
                paused_for = max(0, now - paused_at)
                state["deadline"] += paused_for
                state["paused"] = False
                state["paused_at"] = None
                state["last_now"] = now
            else:
                state["last_now"] = max(state["last_now"], now)
            return copy.deepcopy(state)

    def set_rule(self, rule):
        with self._lock:
            if rule not in self._MODES:
                raise LootError("INVALID_RULE", rule)
            # Rolls already opened carry an immutable snapshot of their mode.
            self._rule = rule

    def get_roll(self, drop_id):
        with self._lock:
            return copy.deepcopy(self._get(drop_id))

    def loot_item(self, drop_id, character):
        with self._lock:
            state = self._get(drop_id)
            if state["mode"] != "free_for_all":
                return {
                    "looted": False,
                    "reason": "MODE_NOT_FREE_FOR_ALL",
                }
            if state["deliveries"] or state["looted_by"] is not None:
                return {
                    "looted": False,
                    "duplicate": True,
                    "looted_by": state["looted_by"],
                }

            character_id = self._character_id(character)
            roster_entry = self._roster_entry(state, character_id)
            if roster_entry is None and state["eligible"]:
                raise LootError("PLAYER_NOT_ELIGIBLE", character_id)
            if roster_entry is not None and roster_entry.get("departed"):
                raise LootError("PLAYER_NOT_ELIGIBLE", character_id)

            state["looted_by"] = character_id
            state["delivered_to"] = character_id
            state["delivery_method"] = "loot"
            state["deliveries"] = 1
            state["status"] = "delivered"
            return {
                "looted": True,
                "character": character_id,
                "item_id": state["item_id"],
            }

    def outbox(self):
        with self._lock:
            return copy.deepcopy(self._outbox)

    def dump(self):
        with self._lock:
            return copy.deepcopy({
                "rule": self._rule,
                "rolls": self._rolls,
                "outbox": self._outbox,
            })

    def load(self, state):
        with self._lock:
            if isinstance(state, str):
                import json
                state = json.loads(state)
            self._rule = state.get("rule", self._rule)
            loaded_rolls = state.get("rolls", {})
            self._rolls = {}
            for key, roll in loaded_rolls.items():
                roll = copy.deepcopy(roll)
                drop_id = roll.get("drop_id", key)
                roll["drop_id"] = drop_id
                # JSON turns integer mapping keys into strings.  The
                # authoritative id is stored inside each roster entry and
                # in drop_id, so rebuild the indexes from those values.
                roster = roll.get("eligible", {})
                if isinstance(roster, dict):
                    restored = {}
                    for roster_key, entry in roster.items():
                        character_id = entry.get("id", roster_key)
                        entry["id"] = character_id
                        restored[character_id] = entry
                    roll["eligible"] = restored
                self._rolls[drop_id] = roll
            self._outbox = copy.deepcopy(state.get("outbox", []))
            for roll in self._rolls.values():
                roll.setdefault("last_now", 0)
                roll.setdefault("paused", False)
                roll.setdefault("paused_at", None)
                roll.setdefault("deliveries", 0)
                roll.setdefault("mailed", False)
                roll.setdefault("looted_by", None)

    # ------------------------------------------------------------------
    # Optional roster events.  These make disconnect/leave explicit for
    # callers that receive such events from the session layer.
    def disconnect(self, drop_id, character):
        return self._set_roster_flag(drop_id, character, online=False)

    def reconnect(self, drop_id, character):
        return self._set_roster_flag(drop_id, character, online=True)

    def leave(self, drop_id, character):
        return self._set_roster_flag(drop_id, character, departed=True)

    def _set_roster_flag(self, drop_id, character, **flags):
        with self._lock:
            state = self._get(drop_id)
            character_id = self._character_id(character)
            entry = state["eligible"].get(character_id)
            if entry is None:
                raise LootError("PLAYER_NOT_ELIGIBLE", character_id)
            entry.update(flags)
            return copy.deepcopy(entry)

    # ------------------------------------------------------------------
    # Internals
    def _get(self, drop_id):
        try:
            return self._rolls[drop_id]
        except KeyError:
            raise LootError("DROP_NOT_FOUND", drop_id)

    @staticmethod
    def _character_id(character):
        if isinstance(character, dict):
            for key in ("id", "character_id", "character"):
                if key in character:
                    return character[key]
        return character

    @classmethod
    def _normalize_eligible(cls, eligible):
        roster = {}
        if isinstance(eligible, (str, bytes)) or isinstance(eligible, dict):
            eligible = (eligible,)
        for entry in eligible or ():
            if isinstance(entry, dict):
                character_id = cls._character_id(entry)
                can_need = bool(entry.get("can_need", True))
                online = entry.get(
                    "online", entry.get(
                        "connected", not entry.get(
                            "offline", entry.get("disconnected", False))))
                departed = bool(entry.get(
                    "departed", entry.get("left", False)))
            else:
                character_id = entry
                can_need = True
                online = True
                departed = False
            if character_id is None:
                continue
            roster[character_id] = {
                "id": character_id,
                "can_need": can_need,
                "online": bool(online),
                "departed": departed,
            }
        return roster

    @staticmethod
    def _roster_entry(state, character_id):
        return state["eligible"].get(character_id)

    @staticmethod
    def _expired(state, now):
        return state["deadline"] is not None and now > state["deadline"]

    def _settle(self, state, now):
        if state["status"] != "open":
            return

        state["status"] = "settled"
        state["settled_at"] = now
        state["last_now"] = max(state["last_now"], now)
        state["paused"] = False
        state["paused_at"] = None

        if state["mode"] == "free_for_all":
            state["winner"] = None
            state["tie"] = False
            return

        if state["mode"] == "captain":
            # Captain loot is settled by assign(); closing only freezes
            # voting and prevents any later vote from changing the record.
            state["winner"] = None
            state["tie"] = False
            return

        candidates = []
        for vote in state["votes"]:
            entry = state["eligible"].get(vote["character"])
            if entry is not None and (
                    entry.get("departed") or entry.get("online") is False):
                continue
            priority = 0 if vote["kind"] == "need" else 1
            candidates.append((priority, vote))

        if not candidates:
            state["winner"] = None
            state["tie"] = False
            return

        if any(item[0] == 0 for item in candidates):
            candidates = [item for item in candidates if item[0] == 0]

        candidates.sort(
            key=lambda item: (
                item[0],
                -item[1]["total"],
                self._secondary_key(item[1]["character"]),
            )
        )

        top_priority = candidates[0][0]
        top_total = candidates[0][1]["total"]
        tied = [
            item for item in candidates
            if item[0] == top_priority and item[1]["total"] == top_total
        ]

        state["winner"] = candidates[0][1]["character"]
        state["tie"] = len(tied) > 1
        state["tie_break"] = state["winner"] if state["tie"] else None

    @staticmethod
    def _secondary_key(character_id):
        # Character ids are the stable identity across servers.  Sorting a
        # tie by that id makes both UI views agree without random choice.
        if isinstance(character_id, bool):
            return (1, str(character_id))
        if isinstance(character_id, (int, float)):
            return (0, character_id)
        return (1, str(character_id))

    def _mail(self, state, recipient, reason):
        entry = {
            "character": recipient,
            "item_id": state["item_id"],
            "source": state["drop_id"],
            "drop_id": state["drop_id"],
            "source_drop_id": state["drop_id"],
            "reason": reason,
        }
        self._outbox.append(entry)
        state["deliveries"] = 1
        state["delivered_to"] = recipient
        state["delivery_method"] = "mail"
        state["mailed"] = True
        state["status"] = "delivered"

        if self.mailer is None:
            return
        if callable(self.mailer):
            self._invoke_mailer(self.mailer, entry)
            return
        for method_name in ("send", "send_mail", "mail", "enqueue"):
            method = getattr(self.mailer, method_name, None)
            if callable(method):
                self._invoke_mailer(method, entry)
                return

    @staticmethod
    def _invoke_mailer(method, entry):
        try:
            method(copy.deepcopy(entry))
        except TypeError:
            try:
                method(entry["character"], entry["item_id"],
                       source=entry["source"])
            except TypeError:
                method(entry["character"], entry["item_id"],
                       entry["source"])
