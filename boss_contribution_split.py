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
        self._seq = 0
        self._battles = {}
        self._active = {}
        self._mail = {}

    def start_fight(self, boss_id, shard="s1", now=0):
        key = (boss_id, shard)
        existing = self._active.get(key)
        if existing is not None:
            return {"status": "existing", "battle_id": existing}
        self._seq += 1
        battle_id = "{}-{}-{}".format(boss_id, shard, self._seq)
        self._battles[battle_id] = {
            "battle_id": battle_id,
            "boss_id": boss_id,
            "shard": shard,
            "started_at": now,
            "finished_at": None,
            "settled_at": None,
            "players": {},
            "join_order": [],
            "kill_by": None,
        }
        self._active[key] = battle_id
        return {"status": "started", "battle_id": battle_id}

    def join(self, battle_id, player, server="s1", now=0):
        battle = self._battle(battle_id)
        self._require_active(battle)
        row = battle["players"].get(player)
        if row is None:
            row = {
                "player": player,
                "server": server,
                "online": True,
                "damage": 0,
                "heal": 0,
                "mitigation": 0,
                "contribution": 0,
                "joined_at": now,
            }
            battle["players"][player] = row
            battle["join_order"].append(player)
        return {"status": "joined", "battle_id": battle_id, "player": player}

    def record_damage(self, battle_id, player, amount, pet=False, now=0):
        row = self._record_row(battle_id, player)
        # Pet hits are folded into the owner's own damage total, so a pet
        # hit is counted exactly once rather than once for pet + once owner.
        row["damage"] += amount
        self._refresh_contribution(row)
        return {"status": "recorded", "player": player, "damage": row["damage"]}

    def record_heal(self, battle_id, player, amount, now=0):
        row = self._record_row(battle_id, player)
        row["heal"] += amount
        self._refresh_contribution(row)
        return {"status": "recorded", "player": player, "heal": row["heal"]}

    def record_mitigation(self, battle_id, player, amount, now=0):
        row = self._record_row(battle_id, player)
        row["mitigation"] += amount
        self._refresh_contribution(row)
        return {"status": "recorded", "player": player,
                "mitigation": row["mitigation"]}

    def record_kill(self, battle_id, player, now=0):
        battle = self._battle(battle_id)
        self._require_active(battle)
        if player not in battle["players"]:
            raise BossError("player_not_joined", player)
        battle["kill_by"] = player
        return {"status": "recorded", "battle_id": battle_id, "player": player}

    def disconnect(self, battle_id, player, now=0):
        battle = self._battle(battle_id)
        row = battle["players"].get(player)
        if row is None:
            raise BossError("player_not_joined", player)
        row["online"] = False
        return {"status": "disconnected", "player": player}

    def reconnect(self, battle_id, player, now=0):
        battle = self._battle(battle_id)
        self._require_active(battle)
        row = battle["players"].get(player)
        if row is None:
            raise BossError("player_not_joined", player)
        row["online"] = True
        return {"status": "reconnected", "player": player}

    def finish(self, battle_id, now=0):
        battle = self._battle(battle_id)
        if battle["finished_at"] is not None:
            return {"status": "already_finished", "battle_id": battle_id}
        battle["finished_at"] = now
        return {"status": "finished", "battle_id": battle_id}

    def settle(self, battle_id, now=0):
        battle = self._battle(battle_id)
        if battle["finished_at"] is None:
            raise BossError("fight_active", battle_id)
        if battle["settled_at"] is not None:
            # Same battle_id can only settle once: repeat calls must never
            # produce a second round of reward mails.
            raise BossError("already_settled", battle_id)

        eligible = [battle["players"][name]
                    for name in battle["join_order"]
                    if self._eligible(battle["players"][name])]
        total = sum(row["contribution"] for row in eligible)
        pool = int(self.reward_pool)
        shares = self._split_pool(pool, eligible, total)

        # Eligibility decision, reward split and mail delivery are committed
        # together: the settled flag flips only after every mail is staged,
        # so a failure leaves the fight unsettled instead of half-paid.
        staged = []
        for row, reward in zip(eligible, shares):
            staged.append({
                "battle_id": battle_id,
                "boss_id": battle["boss_id"],
                "player": row["player"],
                "server": row["server"],
                "damage": row["damage"],
                "heal": row["heal"],
                "mitigation": row["mitigation"],
                "contribution": row["contribution"],
                "reward": reward,
                "at": now,
            })
        battle["settled_at"] = now
        battle["rewards"] = [
            {"player": item["player"], "reward": item["reward"],
             "contribution": item["contribution"]}
            for item in staged
        ]
        for item in staged:
            self._mail.setdefault(item["player"], []).append(item)
        self._active.pop((battle["boss_id"], battle["shard"]), None)
        return {"status": "settled", "battle_id": battle_id,
                "rewards": list(battle["rewards"])}

    def contributions(self, battle_id):
        battle = self._battle(battle_id)
        return {
            name: {
                "player": name,
                "server": row["server"],
                "online": row["online"],
                "damage": row["damage"],
                "heal": row["heal"],
                "mitigation": row["mitigation"],
                "contribution": row["contribution"],
            }
            for name, row in battle["players"].items()
        }

    def leaderboard(self, battle_id):
        battle = self._battle(battle_id)
        ordered = sorted(
            (row for row in battle["players"].values()
             if self._eligible(row)),
            key=lambda row: (-row["contribution"],
                             battle["join_order"].index(row["player"])))
        return [
            {
                "player": row["player"],
                "server": row["server"],
                "damage": row["damage"],
                "heal": row["heal"],
                "mitigation": row["mitigation"],
                "contribution": row["contribution"],
            }
            for row in ordered
        ]

    def rewards(self, battle_id):
        battle = self._battle(battle_id)
        return list(battle.get("rewards", []))

    def mailbox(self, player=None):
        if player is None:
            return [item for items in self._mail.values() for item in items]
        return list(self._mail.get(player, []))

    def dump(self):
        return {
            "seq": self._seq,
            "weights": dict(self.weights),
            "participation_min": self.participation_min,
            "reward_pool": self.reward_pool,
            "active": {
                "{}|{}".format(boss_id, shard): battle_id
                for (boss_id, shard), battle_id in self._active.items()
            },
            "battles": self._battles,
            "mail": self._mail,
        }

    def load(self, state):
        self._seq = state.get("seq", 0)
        self.weights = dict(state.get("weights", self.weights))
        self.participation_min = state.get(
            "participation_min", self.participation_min)
        self.reward_pool = state.get("reward_pool", self.reward_pool)
        self._battles = state.get("battles", {})
        self._mail = state.get("mail", {})
        self._active = {}
        for encoded, battle_id in state.get("active", {}).items():
            boss_id, shard = encoded.split("|", 1)
            self._active[(boss_id, shard)] = battle_id
        # Rebuild active index from battle state as well, so a snapshot
        # without an explicit active map still blocks duplicate settlements.
        for battle_id, battle in self._battles.items():
            if battle.get("settled_at") is None:
                self._active.setdefault(
                    (battle["boss_id"], battle["shard"]), battle_id)
        return {"status": "loaded"}

    def _battle(self, battle_id):
        battle = self._battles.get(battle_id)
        if battle is None:
            raise BossError("battle_not_found", battle_id)
        return battle

    @staticmethod
    def _require_active(battle):
        if battle["finished_at"] is not None:
            raise BossError("fight_ended", battle["battle_id"])

    def _record_row(self, battle_id, player):
        battle = self._battle(battle_id)
        self._require_active(battle)
        row = battle["players"].get(player)
        if row is None:
            raise BossError("player_not_joined", player)
        if not row["online"]:
            # Disconnected players keep everything earned so far, but no
            # further records are accepted until they reconnect.
            raise BossError("player_offline", player)
        return row

    def _refresh_contribution(self, row):
        row["contribution"] = (
            row["damage"] * self.weights["damage"]
            + row["heal"] * self.weights["heal"]
            + row["mitigation"] * self.weights["mitigation"]
        )

    def _eligible(self, row):
        return (row["damage"] + row["heal"] + row["mitigation"]
                >= self.participation_min)

    @staticmethod
    def _split_pool(pool, eligible, total):
        if not eligible or total <= 0:
            return [0 for _ in eligible]
        raw = [pool * row["contribution"] / total for row in eligible]
        floors = [int(value) for value in raw]
        remainder = pool - sum(floors)
        order = sorted(
            range(len(eligible)),
            key=lambda index: (-(raw[index] - floors[index]), index))
        for index in order[:remainder]:
            floors[index] += 1
        return floors
