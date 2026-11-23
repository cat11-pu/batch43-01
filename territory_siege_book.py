class SiegeError(ValueError):
    """Raised when a territory declaration or settlement is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TerritorySiegeBook:
    """Territory declarations, siege settlement, ownership and tax ticks."""

    def __init__(self, declare_cost=100, window=60, tax_interval=100,
                 tax_amount=10):
        self.declare_cost = declare_cost
        self.window = window
        self.tax_interval = tax_interval
        self.tax_amount = tax_amount
        self._territories = {}
        self._accounts = {}
        self._scores = {}
        self._settlements = {}
        self._battle_results = set()

    def create_territory(self, territory_id, owner, buildings=None,
                         permission=None):
        if permission is None:
            permission = [owner] if owner is not None else []
        self._territories[territory_id] = {
            "id": territory_id,
            "owner": owner,
            "buildings": list(buildings or []),
            "permission": list(permission),
            "contest": None,
            "last_tax_at": 0,
        }

    def fund(self, guild, amount):
        self._accounts[guild] = self._accounts.get(guild, 0) + amount

    def declare(self, territory, guild, now=0, cost=None):
        record = self._territories[territory]
        cost = self.declare_cost if cost is None else cost

        contest = record["contest"]
        if contest is not None and now - contest["declared_at"] < self.window:
            raise SiegeError(
                "already_contested",
                {"territory": territory, "guild": contest["guild"]},
            )

        balance = self._accounts.get(guild, 0)
        if balance < cost:
            raise SiegeError(
                "insufficient_funds",
                {"balance": balance, "cost": cost},
            )

        # All validation happens before any state mutation, so a rejected
        # declaration never moves money: the charge and the opening of the
        # contest are a single atomic operation.
        self._accounts[guild] = balance - cost
        record["contest"] = {"guild": guild, "declared_at": now, "cost": cost}
        return {"status": "declared", "territory": territory, "guild": guild}

    def settle(self, territory, winner, now=0, idempotency_key=None,
               defender_qualified=True):
        record = self._territories[territory]

        if idempotency_key is not None and idempotency_key in self._settlements:
            return dict(self._settlements[idempotency_key], status="replayed")

        contest = record["contest"]
        if contest is None or now - contest["declared_at"] < self.window:
            raise SiegeError(
                "window_open" if contest is not None else "not_contested",
                {"territory": territory},
            )

        previous_owner = record["owner"]
        if winner == previous_owner and not defender_qualified:
            raise SiegeError(
                "defender_disqualified",
                {"territory": territory, "winner": winner},
            )

        # Ownership change, building/permission cleanup, scoring and the
        # idempotency record all commit together against the in-memory state,
        # which snapshot()/restore() makes durable across restarts.
        if winner != previous_owner:
            record["owner"] = winner
            record["buildings"] = []
            record["permission"] = [winner]
        record["contest"] = None
        self._scores[winner] = self._scores.get(winner, 0) + 1

        result = {
            "status": "settled",
            "territory": territory,
            "winner": winner,
            "previous_owner": previous_owner,
            "settled_at": now,
        }
        if idempotency_key is not None:
            self._settlements[idempotency_key] = dict(result)
        return dict(result)

    def transfer(self, territory, guild, actor, now=0):
        record = self._territories[territory]
        previous_owner = record["owner"]
        # Ownership, buildings and permissions change in one transaction so a
        # reused territory can never keep the previous lord's garrison.
        record["owner"] = guild
        record["buildings"] = []
        record["permission"] = [guild]
        record["contest"] = None
        return {
            "status": "transferred",
            "territory": territory,
            "from": previous_owner,
            "to": guild,
            "actor": actor,
            "transferred_at": now,
        }

    def tick(self, now=0):
        paid = 0
        for record in self._territories.values():
            owner = record["owner"]
            last_tax_at = record["last_tax_at"]
            if now < last_tax_at:
                continue
            periods = (now - last_tax_at) // self.tax_interval
            if periods <= 0:
                continue
            amount = periods * self.tax_amount
            if owner is not None:
                self._accounts[owner] = self._accounts.get(owner, 0) + amount
                paid += amount
            # Advance the tax clock regardless of ownership so taxes accrued
            # before a transfer are never paid out twice.
            record["last_tax_at"] = last_tax_at + periods * self.tax_interval
        return {"now": now, "tax_paid": paid}

    def battle_result(self, battlefield, guild, score, player=None, now=0):
        key = (battlefield, player if player is not None else guild)
        if key in self._battle_results:
            return {"status": "duplicate_result", "battlefield": battlefield,
                    "guild": guild, "player": player}
        self._battle_results.add(key)
        self._scores[guild] = self._scores.get(guild, 0) + score
        return {"status": "recorded", "battlefield": battlefield,
                "guild": guild, "player": player, "score": score}

    def territory(self, territory):
        record = self._territories[territory]
        return {
            "id": record["id"],
            "owner": record["owner"],
            "buildings": list(record["buildings"]),
            "permission": list(record["permission"]),
            "contest": dict(record["contest"]) if record["contest"] else None,
            "last_tax_at": record["last_tax_at"],
        }

    def guild_account(self, guild):
        return self._accounts.get(guild, 0)

    def guild_score(self, guild):
        return self._scores.get(guild, 0)

    def season_reset(self):
        for record in self._territories.values():
            record["owner"] = None
            record["buildings"] = []
            record["permission"] = []
            record["contest"] = None
            record["last_tax_at"] = 0
        self._scores.clear()
        self._settlements.clear()
        self._battle_results.clear()
        return {"leaderboard_cleared": True,
                "territories_cleared": len(self._territories)}

    def snapshot(self):
        import copy
        return {
            "declare_cost": self.declare_cost,
            "window": self.window,
            "tax_interval": self.tax_interval,
            "tax_amount": self.tax_amount,
            "territories": copy.deepcopy(self._territories),
            "accounts": dict(self._accounts),
            "scores": dict(self._scores),
            "settlements": copy.deepcopy(self._settlements),
            "battle_results": list(self._battle_results),
        }

    def restore(self, state):
        import copy
        self.declare_cost = state["declare_cost"]
        self.window = state["window"]
        self.tax_interval = state["tax_interval"]
        self.tax_amount = state["tax_amount"]
        self._territories = copy.deepcopy(state["territories"])
        self._accounts = dict(state["accounts"])
        self._scores = dict(state["scores"])
        self._settlements = copy.deepcopy(state.get("settlements", {}))
        self._battle_results = set(state.get("battle_results", ()))
