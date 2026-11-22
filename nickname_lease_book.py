class NicknameError(ValueError):
    """Raised when a nickname lease operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class NicknameLeaseBook:
    """Nickname claims, renames, reservations and account restoration."""

    def __init__(self, cooldown=1000, reservation=5000):
        self.cooldown = cooldown
        self.reservation = reservation

    def grant_card(self, account, count=1):
        raise NotImplementedError

    def cards(self, account):
        raise NotImplementedError

    def seed(self, account, nickname, now=0):
        raise NotImplementedError

    def claim(self, account, nickname, now=0, card=True):
        raise NotImplementedError

    def rename(self, account, nickname, now=0, card=True):
        raise NotImplementedError

    def release(self, account, now=0):
        raise NotImplementedError

    def delete_account(self, account, now=0):
        raise NotImplementedError

    def restore(self, account, now=0):
        raise NotImplementedError

    def force_rename(self, account, nickname, actor, now=0):
        raise NotImplementedError

    def merge(self, accounts, now=0):
        raise NotImplementedError

    def resolve(self, nickname):
        raise NotImplementedError

    def nickname_of(self, account):
        raise NotImplementedError

    def history(self, account):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
