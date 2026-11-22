class ReputeError(ValueError):
    """Raised when a reputation operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FactionReputeLedger:
    """Faction reputation bounds, vendor unlocks, switches and ledger."""

    def __init__(self, factions=None):
        self.factions = factions or {}

    def define_faction(self, faction, minimum=0, maximum=1000,
                       hostile_below=0, vendors=None):
        raise NotImplementedError

    def grant(self, character, faction, source, amount, now=0,
              multiplier=1.0, bonus_pct=0):
        raise NotImplementedError

    def adjust(self, character, faction, amount, actor, reason, now=0):
        raise NotImplementedError

    def switch_faction(self, character, from_faction, to_faction, now=0):
        raise NotImplementedError

    def reputation(self, character, faction):
        raise NotImplementedError

    def standing(self, character, faction):
        raise NotImplementedError

    def vendors(self, character, faction):
        raise NotImplementedError

    def purchase(self, character, faction, item, now=0):
        raise NotImplementedError

    def purchases(self, character):
        raise NotImplementedError

    def weekly_decay(self, character, faction, week, amount=10, now=0):
        raise NotImplementedError

    def ledger(self, character=None, faction=None):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
