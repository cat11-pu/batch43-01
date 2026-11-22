class PurgeError(ValueError):
    """Raised when a character purge or restore operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CharPurgeRestore:
    """Transactional character deletion, sweeping and restoration."""

    def __init__(self, fee=100, reservation=5000, max_restored=2):
        self.fee = fee
        self.reservation = reservation
        self.max_restored = max_restored

    def fund_account(self, account, amount):
        raise NotImplementedError

    def account_funds(self, account):
        raise NotImplementedError

    def create_character(self, character_id, account, nickname,
                         guild_rank="member", permissions=None, inventory=None,
                         mail=None, achievements=None, now=0):
        raise NotImplementedError

    def purge(self, character_id, actor, now=0, fail_step=None,
              crash_after=None):
        raise NotImplementedError

    def restore(self, character_id, account, now=0):
        raise NotImplementedError

    def sweep(self, now=0):
        raise NotImplementedError

    def characters(self, account=None):
        raise NotImplementedError

    def search(self, nickname):
        raise NotImplementedError

    def leaderboard(self):
        raise NotImplementedError

    def inventory(self, character_id):
        raise NotImplementedError

    def mailbox(self, character_id):
        raise NotImplementedError

    def transfer_item(self, from_character, to_character, item, count=1):
        raise NotImplementedError

    def claim_nickname(self, account, nickname, now=0):
        raise NotImplementedError

    def login(self, character_id, now=0):
        raise NotImplementedError

    def season_rewards(self, amount, now=0):
        raise NotImplementedError

    def account_achievements(self, account):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
