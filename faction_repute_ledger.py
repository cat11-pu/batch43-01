import threading


class ReputeError(ValueError):
    """Raised when a reputation operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FactionReputeLedger:
    """Faction reputation bounds, vendor unlocks, switches and ledger."""

    def __init__(self, factions=None):
        self.factions = {}
        for name, config in (factions or {}).items():
            self.factions[name] = {
                "minimum": config["minimum"],
                "maximum": config["maximum"],
                "hostile_below": config["hostile_below"],
                "vendors": sorted(
                    ((threshold, list(items))
                     for threshold, items in config.get("vendors", {}).items()),
                    key=lambda entry: entry[0],
                ),
            }
        # reputation[character][faction] -> int
        self._reputation = {}
        # granted[character][faction] -> set of sources already settled
        self._granted = {}
        # decayed[character][faction] -> set of week ids already decayed
        self._decayed = {}
        # purchase records, keyed by character
        self._purchases = {}
        # append-only reputation change log
        self._ledger = []
        self._sequence = 0
        self._lock = threading.RLock()

    def _require_faction(self, faction):
        if faction not in self.factions:
            raise ReputeError("unknown_faction", faction)
        return self.factions[faction]

    def _clamp(self, faction, value):
        config = self._require_faction(faction)
        return max(config["minimum"], min(config["maximum"], value))

    def _record(self, character, faction, kind, amount, before, after,
                now, actor=None, reason=None, **extra):
        self._sequence += 1
        entry = {
            "seq": self._sequence,
            "character": character,
            "faction": faction,
            "kind": kind,
            "amount": amount,
            "before": before,
            "after": after,
            "actor": actor,
            "reason": reason,
            "now": now,
        }
        entry.update(extra)
        self._ledger.append(entry)
        return entry

    def define_faction(self, faction, minimum=0, maximum=1000,
                       hostile_below=0, vendors=None):
        with self._lock:
            if minimum > maximum:
                raise ReputeError(
                    "invalid_bounds",
                    {"minimum": minimum, "maximum": maximum},
                )
            self.factions[faction] = {
                "minimum": minimum,
                "maximum": maximum,
                "hostile_below": hostile_below,
                "vendors": sorted(
                    ((threshold, list(items))
                     for threshold, items in (vendors or {}).items()),
                    key=lambda entry: entry[0],
                ),
            }

    def grant(self, character, faction, source, amount, now=0,
              multiplier=1.0, bonus_pct=0):
        with self._lock:
            self._require_faction(faction)
            granted = self._granted.setdefault(character, {}).setdefault(faction, set())
            if source in granted:
                return {
                    "status": "already_granted",
                    "source": source,
                    "amount": 0,
                    "discarded": 0,
                    "reputation": self._reputation.get(character, {}).get(faction, 0),
                    "now": now,
                }
            # Bonuses stack additively on top of the base amount so that an
            # event multiplier and a guild bonus can never compound.
            effective = int(round(amount * (multiplier + bonus_pct / 100.0)))
            current = self._reputation.get(character, {}).get(faction, 0)
            target = self._clamp(faction, current + effective)
            applied = target - current
            discarded = max(0, effective - applied)
            self._reputation.setdefault(character, {})[faction] = target
            granted.add(source)
            self._record(
                character, faction, "grant", applied, current, target, now,
                actor=source, reason=source, source=source,
                requested=effective, discarded=discarded, multiplier=multiplier,
                bonus_pct=bonus_pct,
            )
            return {
                "status": "granted",
                "source": source,
                "amount": applied,
                "discarded": discarded,
                "reputation": target,
                "now": now,
            }

    def adjust(self, character, faction, amount, actor, reason, now=0):
        with self._lock:
            self._require_faction(faction)
            current = self._reputation.get(character, {}).get(faction, 0)
            target = self._clamp(faction, current + amount)
            applied = target - current
            discarded = abs(amount) - abs(applied)
            self._reputation.setdefault(character, {})[faction] = target
            self._record(
                character, faction, "adjust", applied, current, target, now,
                actor=actor, reason=reason, requested=amount, discarded=discarded,
            )
            return {
                "status": "adjusted",
                "amount": applied,
                "reputation": target,
                "now": now,
            }

    def switch_faction(self, character, from_faction, to_faction, now=0):
        with self._lock:
            self._require_faction(from_faction)
            self._require_faction(to_faction)
            if from_faction == to_faction:
                raise ReputeError(
                    "invalid_switch",
                    {"from_faction": from_faction, "to_faction": to_faction},
                )
            old = self._reputation.get(character, {}).get(from_faction, 0)
            floor = self.factions[from_faction]["minimum"]
            self._reputation.setdefault(character, {})[from_faction] = floor
            # Vendor permissions are derived from live reputation, so clearing
            # the opposing faction revokes every unlock atomically.
            self._record(
                character, from_faction, "switch", floor - old, old, floor, now,
                actor=character, reason="switch_to:%s" % to_faction,
                to_faction=to_faction,
            )
            return {
                "status": "switched",
                "from_faction": from_faction,
                "to_faction": to_faction,
                "reputation": floor,
                "now": now,
            }

    def reputation(self, character, faction):
        with self._lock:
            self._require_faction(faction)
            return self._reputation.get(character, {}).get(
                faction, self.factions[faction]["minimum"]
            )

    def standing(self, character, faction):
        with self._lock:
            config = self._require_faction(faction)
            value = self.reputation(character, faction)
            return "hostile" if value < config["hostile_below"] else "neutral"

    def vendors(self, character, faction):
        with self._lock:
            config = self._require_faction(faction)
            value = self.reputation(character, faction)
            unlocked = []
            for threshold, items in config["vendors"]:
                if value >= threshold:
                    unlocked.extend(items)
            return unlocked

    def purchase(self, character, faction, item, now=0):
        with self._lock:
            self._require_faction(faction)
            if item not in self.vendors(character, faction):
                raise ReputeError(
                    "vendor_locked",
                    {"character": character, "faction": faction, "item": item},
                )
            record = {
                "character": character,
                "faction": faction,
                "item": item,
                "now": now,
                "revoked": False,
            }
            self._purchases.setdefault(character, []).append(record)
            self._record(
                character, faction, "purchase", 0,
                self.reputation(character, faction),
                self.reputation(character, faction), now,
                actor=character, reason="purchase:%s" % item, item=item,
            )
            return dict(record)

    def purchases(self, character):
        with self._lock:
            records = []
            for record in self._purchases.get(character, []):
                view = dict(record)
                view["revoked"] = self._is_revoked(
                    record["character"], record["faction"], record["item"]
                )
                records.append(view)
            return records

    def _is_revoked(self, character, faction, item):
        if faction not in self.factions:
            return True
        return item not in self.vendors(character, faction)

    def weekly_decay(self, character, faction, week, amount=10, now=0):
        with self._lock:
            self._require_faction(faction)
            weeks = self._decayed.setdefault(character, {}).setdefault(faction, set())
            current = self.reputation(character, faction)
            if week in weeks:
                return {
                    "status": "already_applied",
                    "week": week,
                    "amount": 0,
                    "reputation": current,
                    "now": now,
                }
            target = self._clamp(faction, current - amount)
            applied = target - current
            self._reputation.setdefault(character, {})[faction] = target
            weeks.add(week)
            self._record(
                character, faction, "decay", applied, current, target, now,
                actor="system", reason="weekly_decay:%s" % week, week=week,
            )
            return {
                "status": "applied",
                "week": week,
                "amount": applied,
                "reputation": target,
                "now": now,
            }

    def ledger(self, character=None, faction=None):
        with self._lock:
            return [
                dict(entry)
                for entry in self._ledger
                if (character is None or entry["character"] == character)
                and (faction is None or entry["faction"] == faction)
            ]

    def snapshot(self):
        with self._lock:
            import copy
            return copy.deepcopy({
                "factions": self.factions,
                "reputation": self._reputation,
                "granted": {
                    character: {
                        faction: sorted(sources)
                        for faction, sources in by_faction.items()
                    }
                    for character, by_faction in self._granted.items()
                },
                "decayed": {
                    character: {
                        faction: sorted(weeks)
                        for faction, weeks in by_faction.items()
                    }
                    for character, by_faction in self._decayed.items()
                },
                "purchases": self._purchases,
                "ledger": self._ledger,
                "sequence": self._sequence,
            })

    def restore(self, state):
        with self._lock:
            import copy
            state = copy.deepcopy(state)
            self.factions = state["factions"]
            self._reputation = state.get("reputation", {})
            self._granted = {
                character: {
                    faction: set(sources)
                    for faction, sources in by_faction.items()
                }
                for character, by_faction in state.get("granted", {}).items()
            }
            self._decayed = {
                character: {
                    faction: set(weeks)
                    for faction, weeks in by_faction.items()
                }
                for character, by_faction in state.get("decayed", {}).items()
            }
            self._purchases = state.get("purchases", {})
            self._ledger = state.get("ledger", [])
            self._sequence = state.get("sequence", len(self._ledger))
            return self

    def save(self, path):
        """Persist the full ledger to disk as JSON."""
        import json
        with self._lock:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.snapshot(), handle, ensure_ascii=False)

    def load(self, path):
        """Rebuild the ledger from a JSON file written by :meth:`save`."""
        import json
        with open(path, "r", encoding="utf-8") as handle:
            return self.restore(json.load(handle))
