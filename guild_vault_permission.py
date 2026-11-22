class VaultError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class GuildVaultPermission:
    """Guild vault members, ranks, inventory and audit log."""

    def __init__(self, default_daily_limit=None):
        self.default_daily_limit = default_daily_limit

    def add_member(self, guild_id, character_id, rank, account_id=None):
        raise NotImplementedError

    def set_rank(self, guild_id, character_id, rank):
        raise NotImplementedError

    def set_daily_limit(self, guild_id, rank, limit):
        raise NotImplementedError

    def add_slot(self, guild_id, slot, item_id, quantity, bound=False,
                 locked=False, min_rank=0):
        raise NotImplementedError

    def deposit(self, guild_id, character_id, slot, item_id, quantity,
                bound=False, now=0):
        raise NotImplementedError

    def withdraw(self, guild_id, character_id, slot, quantity, now=0):
        raise NotImplementedError

    def expand(self, guild_id, slots, template_slot=0):
        raise NotImplementedError

    def kick_member(self, guild_id, character_id):
        raise NotImplementedError

    def transfer_leader(self, guild_id, new_leader):
        raise NotImplementedError

    def disband(self, guild_id, actor=None):
        raise NotImplementedError

    def vault(self, guild_id):
        raise NotImplementedError

    def log(self, guild_id):
        raise NotImplementedError

    def outbox(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
