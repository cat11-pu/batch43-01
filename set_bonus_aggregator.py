class SetBonusError(ValueError):
    """Raised for invalid set definitions or equipment operations."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_PCT_SUFFIX = "_pct"


class SetBonusAggregator:
    """Counts equipped set pieces and rebuilds set bonuses as one batch."""

    def __init__(self):
        self._sets = {}
        self._aliases = {}
        self._equipment = {}
        self._stored = {}
        self._locked = set()
        self._preview = None

    def define_set(self, set_id, bonuses, aliases=None, pvp_allowed=True):
        if not set_id or not isinstance(set_id, str):
            raise SetBonusError("invalid_set_id", set_id)
        if not isinstance(bonuses, dict) or not bonuses:
            raise SetBonusError("invalid_bonuses", set_id)
        normalized = {}
        for threshold, stats in bonuses.items():
            if not isinstance(threshold, int) or threshold <= 0:
                raise SetBonusError("invalid_threshold", threshold)
            if not isinstance(stats, dict):
                raise SetBonusError("invalid_bonus_stats", threshold)
            for stat, value in stats.items():
                if not isinstance(value, (int, float)):
                    raise SetBonusError("invalid_bonus_value", stat)
            normalized[threshold] = dict(stats)
        self._sets[set_id] = {
            "bonuses": normalized,
            "pvp_allowed": bool(pvp_allowed),
        }
        for alias in aliases or []:
            self._aliases[alias] = set_id
        self._preview = None

    def equip(self, slot, item):
        if not slot or not isinstance(slot, str):
            raise SetBonusError("invalid_slot", slot)
        if not isinstance(item, dict) or not item.get("item_id"):
            raise SetBonusError("invalid_item", item)
        self._equipment[slot] = dict(item)
        self._preview = None

    def unequip(self, slot):
        self._equipment.pop(slot, None)
        self._preview = None

    def store(self, item):
        if not isinstance(item, dict) or not item.get("item_id"):
            raise SetBonusError("invalid_item", item)
        self._stored[item["item_id"]] = dict(item)

    def lock(self, item_id, locked=True):
        if locked:
            self._locked.add(item_id)
        else:
            self._locked.discard(item_id)
        self._preview = None

    def recompute(self, context="pve"):
        return self._snapshot(self._loadout(), context)

    def stats(self, context="pve"):
        return self.recompute(context)["stats"]

    def active_tiers(self, context="pve"):
        return self.recompute(context)["tiers"]

    def counts(self, context="pve"):
        return self.recompute(context)["counts"]

    def preview(self, loadout, context="pve"):
        entries = []
        for entry in loadout or []:
            item = entry.get("item") if isinstance(entry, dict) else None
            if isinstance(item, dict) and item.get("item_id"):
                entries.append(item)
        self._preview = self._snapshot(entries, context)
        return dict(self._preview)

    def clear_preview(self):
        self._preview = None

    def equipment(self):
        return {slot: dict(item) for slot, item in self._equipment.items()}

    def _loadout(self):
        return list(self._equipment.values())

    def _canonical_set(self, set_id):
        if set_id in self._sets:
            return set_id
        return self._aliases.get(set_id)

    def _snapshot(self, loadout, context):
        seen = set()
        counts = {}
        for item in loadout:
            item_id = item.get("item_id")
            if not item_id or item_id in seen or item_id in self._locked:
                continue
            seen.add(item_id)
            canonical = self._canonical_set(item.get("set_id"))
            if canonical is None:
                continue
            counts[canonical] = counts.get(canonical, 0) + 1

        tiers = {}
        for set_id, count in counts.items():
            definition = self._sets[set_id]
            if context == "pvp" and not definition["pvp_allowed"]:
                continue
            thresholds = self._sets[set_id]["bonuses"]
            satisfied = [t for t in thresholds if count >= t]
            if satisfied:
                tiers[set_id] = max(satisfied)

        flat = {}
        pct = {}
        for set_id, tier in tiers.items():
            definition = self._sets[set_id]
            for stat, value in definition["bonuses"][tier].items():
                if stat.endswith(_PCT_SUFFIX):
                    pct[stat] = pct.get(stat, 1.0) * (1.0 + value / 100.0)
                else:
                    flat[stat] = flat.get(stat, 0) + value

        stats = dict(flat)
        for stat, multiplier in pct.items():
            stats[stat] = (multiplier - 1.0) * 100.0

        return {"counts": counts, "tiers": tiers, "stats": stats}
