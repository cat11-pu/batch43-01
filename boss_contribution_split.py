class BossError(ValueError):
    """Raised when a boss fight tracking operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BossContributionSplit:
    """Per-fight contribution tracking and reward distribution."""

    def __init__(self, weights=None, participation_min=1, reward_pool=1000):
        self.weights = weights or {"damage": 1.0, "heal": 0.5,
                                   "mitigation": 0.25}
        self.participation_min = participation_min
        self.reward_pool = reward_pool

    def start_fight(self, boss_id, shard="s1", now=0):
        raise NotImplementedError

    def join(self, battle_id, player, server="s1", now=0):
        raise NotImplementedError

    def record_damage(self, battle_id, player, amount, pet=False, now=0):
        raise NotImplementedError

    def record_heal(self, battle_id, player, amount, now=0):
        raise NotImplementedError

    def record_mitigation(self, battle_id, player, amount, now=0):
        raise NotImplementedError

    def record_kill(self, battle_id, player, now=0):
        raise NotImplementedError

    def disconnect(self, battle_id, player, now=0):
        raise NotImplementedError

    def reconnect(self, battle_id, player, now=0):
        raise NotImplementedError

    def finish(self, battle_id, now=0):
        raise NotImplementedError

    def settle(self, battle_id, now=0):
        raise NotImplementedError

    def contributions(self, battle_id):
        raise NotImplementedError

    def leaderboard(self, battle_id):
        raise NotImplementedError

    def rewards(self, battle_id):
        raise NotImplementedError

    def mailbox(self, player=None):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
