class LadderError(ValueError):
    """Raised when a ladder settlement or season operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class LadderBoardSnapshot:
    """Season ladder ratings, stable ordering and persisted reward snapshots."""

    def __init__(self, season="s1", win_points=16, loss_points=-16):
        self.season = season
        self.win_points = win_points
        self.loss_points = loss_points

    def seed(self, player, points, wins=0):
        raise NotImplementedError

    def settle(self, match_id, winner, loser, now=0):
        raise NotImplementedError

    def ranking(self, limit=None, offset=0, season=None):
        raise NotImplementedError

    def snapshot(self, now=0):
        raise NotImplementedError

    def rewards(self, snapshot_id=None):
        raise NotImplementedError

    def ban(self, player, now=0):
        raise NotImplementedError

    def remove(self, player):
        raise NotImplementedError

    def season_switch(self, season, freeze_at, now=0):
        raise NotImplementedError

    def decay(self, day, amount=5, now=0):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
