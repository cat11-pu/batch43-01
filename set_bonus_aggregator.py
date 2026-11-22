class SetBonusError(ValueError):
    """Raised for invalid set definitions or equipment operations."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SetBonusAggregator:
    """Counts equipped set pieces and rebuilds set bonuses as one batch."""

    def __init__(self):
        pass

    def define_set(self, set_id, bonuses, aliases=None, pvp_allowed=True):
        raise NotImplementedError

    def equip(self, slot, item):
        raise NotImplementedError

    def unequip(self, slot):
        raise NotImplementedError

    def store(self, item):
        raise NotImplementedError

    def lock(self, item_id, locked=True):
        raise NotImplementedError

    def recompute(self, context="pve"):
        raise NotImplementedError

    def stats(self, context="pve"):
        raise NotImplementedError

    def active_tiers(self, context="pve"):
        raise NotImplementedError

    def counts(self, context="pve"):
        raise NotImplementedError

    def preview(self, loadout, context="pve"):
        raise NotImplementedError

    def clear_preview(self):
        raise NotImplementedError

    def equipment(self):
        raise NotImplementedError
