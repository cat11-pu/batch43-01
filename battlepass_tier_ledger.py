class PassError(ValueError):
    """Raised when a battle pass operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BattlepassTierLedger:
    """Seasonal battle pass experience, entitlements and tier redemption."""

    def __init__(self, exp_per_tier=100, tiers=None, season="s1"):
        self.exp_per_tier = exp_per_tier
        self.tiers = tiers or {}
        self.season = season

    def purchase(self, account, order_id, now=0, platform="pc"):
        raise NotImplementedError

    def grant(self, account, amount, source, now=0):
        raise NotImplementedError

    def redeem(self, account, tier, track="free", now=0):
        raise NotImplementedError

    def reclaim(self, account, tier, track="free", now=0):
        raise NotImplementedError

    def refund(self, account, order_id, now=0):
        raise NotImplementedError

    def season_switch(self, new_season, freeze_at, now=0):
        raise NotImplementedError

    def penalty(self, account, tiers, actor, reason, now=0):
        raise NotImplementedError

    def adjust(self, account, tiers, actor, reason, now=0):
        raise NotImplementedError

    def progress(self, account, season=None):
        raise NotImplementedError

    def entitlement(self, account):
        raise NotImplementedError

    def transfer(self, account, server):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
