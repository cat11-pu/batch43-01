class SiegeError(ValueError):
    """Raised when a territory declaration or settlement is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TerritorySiegeBook:
    """Territory declarations, siege settlement, ownership and tax ticks."""

    def __init__(self, declare_cost=100, window=60, tax_interval=100,
                 tax_amount=10):
        self.declare_cost = declare_cost
        self.window = window
        self.tax_interval = tax_interval
        self.tax_amount = tax_amount

    def create_territory(self, territory_id, owner, buildings=None,
                         permission=None):
        raise NotImplementedError

    def fund(self, guild, amount):
        raise NotImplementedError

    def declare(self, territory, guild, now=0, cost=None):
        raise NotImplementedError

    def settle(self, territory, winner, now=0, idempotency_key=None,
               defender_qualified=True):
        raise NotImplementedError

    def transfer(self, territory, guild, actor, now=0):
        raise NotImplementedError

    def tick(self, now=0):
        raise NotImplementedError

    def battle_result(self, battlefield, guild, score, player=None, now=0):
        raise NotImplementedError

    def territory(self, territory):
        raise NotImplementedError

    def guild_account(self, guild):
        raise NotImplementedError

    def guild_score(self, guild):
        raise NotImplementedError

    def season_reset(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
