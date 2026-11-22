class RaidError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RaidSignupWaitlist:
    """Raid roster, role quotas and ordered waitlist."""

    def __init__(self, capacity=None):
        self.default_capacity = capacity or {"tank": 1, "healer": 1, "dps": 3}

    def open_cycle(self, raid_id, capacity, lock_at, now=0):
        raise NotImplementedError

    def signup(self, raid_id, character_id, role, account_id=None, now=0):
        raise NotImplementedError

    def roster(self, raid_id):
        raise NotImplementedError

    def waitlist(self, raid_id):
        raise NotImplementedError

    def promote_next(self, raid_id, role=None):
        raise NotImplementedError

    def change_role(self, raid_id, character_id, role):
        raise NotImplementedError

    def leave(self, raid_id, character_id):
        raise NotImplementedError

    def lock(self, raid_id, now=0):
        raise NotImplementedError

    def is_locked(self, raid_id, now=0):
        raise NotImplementedError

    def join_raid(self, raid_id, character_id):
        raise NotImplementedError

    def record_kill(self, raid_id, boss_id):
        raise NotImplementedError

    def abandon(self, raid_id, character_id):
        raise NotImplementedError

    def cancel_raid(self, raid_id):
        raise NotImplementedError

    def copy_roster(self, source_raid, target_raid, valid_characters):
        raise NotImplementedError

    def snapshot(self, raid_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
