class AllianceError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class GuildAlliancePact:
    """Alliance membership, invitations, wars and merged identities."""

    def __init__(self):
        self.capacity = 10

    def create_alliance(self, tag, leader_guild, capacity=10):
        raise NotImplementedError

    def invite(self, alliance_id, guild_id, now=0, expires_at=0, version=1):
        raise NotImplementedError

    def accept_invite(self, invite_id, guild_id, now=0, version=1):
        raise NotImplementedError

    def leave(self, guild_id):
        raise NotImplementedError

    def disband(self, alliance_id):
        raise NotImplementedError

    def declare_war(self, alliance_id, target_alliance, actor, role="member"):
        raise NotImplementedError

    def record_kill(self, event_id, killer_guild, victim_guild):
        raise NotImplementedError

    def are_friendly(self, guild_a, guild_b):
        raise NotImplementedError

    def post_message(self, alliance_id, guild_id, text):
        raise NotImplementedError

    def channel_history(self, alliance_id, guild_id):
        raise NotImplementedError

    def channel_members(self, alliance_id):
        raise NotImplementedError

    def warehouse_contrib(self, alliance_id, guild_id, amount=0):
        raise NotImplementedError

    def wars(self, alliance_id):
        raise NotImplementedError

    def merge_servers(self, mapping):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
