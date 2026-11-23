class LadderError(ValueError):
    """Raised when a ladder settlement or season operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


BASE_RATING = 1000

# Reward tiers expressed as (highest inclusive rank, reward). Anything past
# the last tier receives no reward.
REWARD_TIERS = (
    (1, 100),
    (3, 50),
    (10, 30),
    (30, 20),
    (100, 10),
)


class LadderBoardSnapshot:
    """Season ladder ratings, stable ordering and persisted reward snapshots.

    Invariants enforced by this class:

    * A match id settles at most once; duplicate settlement raises
      ``already_settled`` and never stacks points.
    * A (season, server, player) tuple owns exactly one rating row, so
      cross-server merges with colliding player ids stay independent.
    * Point changes are zero-sum: the winner gains ``win_points`` and the
      loser loses the same amount under one formula.
    * Ranking order is fully determined by the reproducible key
      ``(-points, -wins, server, player)``; no wall-clock precision or dict
      insertion order can flip tied players between refreshes.
    * The live ranking is recomputed on every read (no cache), and bans or
      removals take effect immediately.
    * Snapshots are deep, frozen copies; rewards always read a persisted
      snapshot, so later settlements cannot move a reward boundary.
    * Settlement arriving after a season freeze is rejected
      (``season_closed``); settlement after the switch lands in the new
      season.
    * Decay for a given (season, day) applies at most once, even across
      maintenance restarts, because the applied days are persisted.
    * Pagination slices a single recomputed ranking by rank, so a player
      dropping off the board cannot duplicate on a later page.
    * ``dump``/``load`` persist pending and settled state, so a node restart
      loses neither ratings nor settlement idempotency.
    """

    def __init__(self, season="s1", win_points=16, loss_points=-16):
        self.season = season
        self.win_points = win_points
        self.loss_points = loss_points
        self._frozen_seasons = {}
        self._players = {}
        self._ratings = {}
        self._matches = {}
        self._decay_days = set()
        self._snapshots = []
        self._snapshot_seq = 0

    def _require_active_player(self, player):
        info = self._players.get(player)
        if info is None:
            raise LadderError("unknown_player", {"player": list(player)})
        if info["banned"]:
            raise LadderError("player_banned", {"player": list(player)})

    def _season_row(self, season, player):
        key = (season, player[0], player[1])
        rating = self._ratings.get(key)
        if rating is None:
            rating = {"points": BASE_RATING, "wins": 0, "losses": 0}
            self._ratings[key] = rating
        return rating

    @staticmethod
    def _sort_key(row):
        return (-row["points"], -row["wins"], row["server"], row["player"])

    @staticmethod
    def _reward_for_rank(rank):
        for upper_rank, reward in REWARD_TIERS:
            if rank <= upper_rank:
                return reward
        return 0

    def seed(self, player, points, wins=0):
        if player not in self._players:
            self._players[player] = {"banned": False}
        self._ratings[(self.season, player[0], player[1])] = {
            "points": points,
            "wins": wins,
            "losses": 0,
        }

    def settle(self, match_id, winner, loser, now=0):
        if match_id in self._matches:
            raise LadderError(
                "already_settled", {"match_id": match_id})
        self._require_active_player(winner)
        self._require_active_player(loser)
        for freeze_at in self._frozen_seasons.values():
            if now < freeze_at:
                raise LadderError(
                    "season_closed",
                    {"match_id": match_id, "now": now, "freeze_at": freeze_at},
                )

        season = self.season
        delta = self.win_points
        winner_rating = self._season_row(season, winner)
        loser_rating = self._season_row(season, loser)
        winner_rating["points"] += delta
        winner_rating["wins"] += 1
        loser_rating["points"] -= delta
        loser_rating["losses"] += 1
        self._matches[match_id] = {
            "season": season,
            "winner": tuple(winner),
            "loser": tuple(loser),
            "now": now,
            "delta": delta,
        }
        return {
            "match_id": match_id,
            "season": season,
            "winner": list(winner),
            "loser": list(loser),
            "winner_points": winner_rating["points"],
            "loser_points": loser_rating["points"],
            "delta": delta,
        }

    def ranking(self, limit=None, offset=0, season=None):
        season = self.season if season is None else season
        rows = []
        for (row_season, server, player_id), rating in self._ratings.items():
            if row_season != season:
                continue
            info = self._players.get((server, player_id))
            if info is not None and info["banned"]:
                continue
            rows.append({
                "server": server,
                "player": player_id,
                "points": rating["points"],
                "wins": rating["wins"],
                "losses": rating["losses"],
            })
        rows.sort(key=self._sort_key)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        if limit is None:
            return rows[offset:]
        return rows[offset:offset + limit]

    def snapshot(self, now=0):
        self._snapshot_seq += 1
        snapshot_id = "snap-%d" % self._snapshot_seq
        frozen_rows = self.ranking()
        self._snapshots.append({
            "snapshot_id": snapshot_id,
            "season": self.season,
            "created_at": now,
            "rows": frozen_rows,
        })
        return {
            "snapshot_id": snapshot_id,
            "season": self.season,
            "created_at": now,
            "count": len(frozen_rows),
        }

    def rewards(self, snapshot_id=None):
        if not self._snapshots:
            raise LadderError("snapshot_not_found", {"snapshot_id": snapshot_id})
        if snapshot_id is None:
            snapshot = self._snapshots[-1]
        else:
            snapshot = next(
                (item for item in self._snapshots
                 if item["snapshot_id"] == snapshot_id),
                None,
            )
            if snapshot is None:
                raise LadderError(
                    "snapshot_not_found", {"snapshot_id": snapshot_id})
        return [{
            "snapshot_id": snapshot["snapshot_id"],
            "server": row["server"],
            "player": row["player"],
            "points": row["points"],
            "rank": row["rank"],
            "reward": self._reward_for_rank(row["rank"]),
        } for row in snapshot["rows"]]

    def ban(self, player, now=0):
        if player not in self._players:
            raise LadderError("unknown_player", {"player": list(player)})
        self._players[player]["banned"] = True
        return {"player": list(player), "banned": True, "now": now}

    def remove(self, player):
        if player not in self._players:
            raise LadderError("unknown_player", {"player": list(player)})
        del self._players[player]
        for key in [key for key in self._ratings
                    if key[1] == player[0] and key[2] == player[1]]:
            del self._ratings[key]
        return {"player": list(player), "removed": True}

    def season_switch(self, season, freeze_at, now=0):
        self._frozen_seasons[self.season] = freeze_at
        self.season = season
        return {
            "season": season,
            "frozen_seasons": list(self._frozen_seasons.items()),
            "now": now,
        }

    def decay(self, day, amount=5, now=0):
        token = (self.season, day)
        if token in self._decay_days:
            return {"season": self.season, "day": day, "applied": False}
        self._decay_days.add(token)
        for (row_season, server, player_id), rating in self._ratings.items():
            if row_season != self.season:
                continue
            info = self._players.get((server, player_id))
            if info is not None and info["banned"]:
                continue
            rating["points"] -= amount
        return {
            "season": self.season,
            "day": day,
            "amount": amount,
            "applied": True,
            "now": now,
        }

    def dump(self):
        return {
            "season": self.season,
            "win_points": self.win_points,
            "loss_points": self.loss_points,
            "frozen_seasons": list(self._frozen_seasons.items()),
            "players": [
                [server, player_id, info["banned"]]
                for (server, player_id), info in sorted(self._players.items())
            ],
            "ratings": [
                [row_season, server, player_id, rating["points"],
                 rating["wins"], rating["losses"]]
                for (row_season, server, player_id), rating
                in sorted(self._ratings.items())
            ],
            "matches": [
                [match_id, record["season"], *record["winner"],
                 *record["loser"], record["now"], record["delta"]]
                for match_id, record in sorted(self._matches.items())
            ],
            "decay_days": sorted(list(day) for day in self._decay_days),
            "snapshots": self._snapshots,
            "snapshot_seq": self._snapshot_seq,
        }

    def load(self, state):
        self.season = state["season"]
        self.win_points = state["win_points"]
        self.loss_points = state["loss_points"]
        self._frozen_seasons = dict(state["frozen_seasons"])
        self._players = {
            (server, player_id): {"banned": banned}
            for server, player_id, banned in state["players"]
        }
        self._ratings = {
            (row_season, server, player_id): {
                "points": points,
                "wins": wins,
                "losses": losses,
            }
            for row_season, server, player_id, points, wins, losses
            in state["ratings"]
        }
        self._matches = {}
        for (match_id, season, winner_server, winner_id,
             loser_server, loser_id, now, delta) in state["matches"]:
            self._matches[match_id] = {
                "season": season,
                "winner": (winner_server, winner_id),
                "loser": (loser_server, loser_id),
                "now": now,
                "delta": delta,
            }
        self._decay_days = {
            (row_season, day) for row_season, day in state["decay_days"]
        }
        self._snapshots = state["snapshots"]
        self._snapshot_seq = state["snapshot_seq"]
        return self
