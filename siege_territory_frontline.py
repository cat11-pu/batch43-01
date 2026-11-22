class SiegeError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SiegeTerritoryFrontline:
    """Stronghold ownership, tick scoring and atomic handover."""

    def __init__(self):
        self.window = None

    def register_stronghold(self, stronghold_id, owner=None):
        raise NotImplementedError

    def start_siege(self, stronghold_id, attackers, defenders, window_start,
                    window_end):
        raise NotImplementedError

    def tick(self, stronghold_id, tick_id, damage_by_guild, now=0):
        raise NotImplementedError

    def end_window(self, stronghold_id, now=0):
        raise NotImplementedError

    def record_kill(self, stronghold_id, event_id, killer_guild,
                    source="player"):
        raise NotImplementedError

    def respawn_point(self, stronghold_id, guild_id):
        raise NotImplementedError

    def buff_affects(self, stronghold_id, guild_id):
        raise NotImplementedError

    def disband_guild(self, guild_id):
        raise NotImplementedError

    def owner(self, stronghold_id):
        raise NotImplementedError

    def progress(self, stronghold_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
