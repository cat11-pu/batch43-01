class InviteError(ValueError):
    """Raised when a party invite operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class InvitePendingBook:
    """Party invites with expiry, account routing and atomic acceptance."""

    def __init__(self, ttl=60, decline_cooldown=30):
        self.ttl = ttl
        self.decline_cooldown = decline_cooldown

    def create_team(self, team_id, leader, capacity=5, now=0):
        raise NotImplementedError

    def disband(self, team_id, actor, now=0):
        raise NotImplementedError

    def block(self, account, target):
        raise NotImplementedError

    def send(self, invite_id, team_id, from_account, to_account, now=0):
        raise NotImplementedError

    def accept(self, invite_id, account, now=0, in_dungeon=False):
        raise NotImplementedError

    def decline(self, invite_id, account, now=0):
        raise NotImplementedError

    def expire(self, invite_id, now=0):
        raise NotImplementedError

    def pending(self, account, now=0):
        raise NotImplementedError

    def team(self, team_id):
        raise NotImplementedError

    def members(self, team_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
