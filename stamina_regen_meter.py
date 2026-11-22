class StaminaError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class StaminaRegenMeter:
    """Timestamped stamina ledger with offline regeneration."""

    def __init__(self, cap=100, regen_seconds=300, wallet=None,
                 potion_restore=50):
        self.cap = cap
        self.regen_seconds = regen_seconds
        self.wallet = wallet
        self.potion_restore = potion_restore

    def grant(self, account, amount, now=0, source="mail"):
        raise NotImplementedError

    def purchase(self, account, amount, now=0):
        raise NotImplementedError

    def consume(self, account, amount, txn_id, now=0, expected=None):
        raise NotImplementedError

    def refund(self, account, txn_id, now=0):
        raise NotImplementedError

    def tick(self, account, now=0):
        raise NotImplementedError

    def set_cap(self, account, new_cap, mode="fill_delta", now=0):
        raise NotImplementedError

    def set_maintenance(self, paused, now=0):
        raise NotImplementedError

    def daily_bonus(self, account, day, amount=10, now=0):
        raise NotImplementedError

    def use_potion(self, account, potions, now=0):
        raise NotImplementedError

    def snapshot(self, account):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
