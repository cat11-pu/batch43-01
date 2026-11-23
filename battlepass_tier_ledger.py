import copy
import threading


class PassError(ValueError):
    """Raised when a battle pass operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BattlepassTierLedger:
    """Seasonal battle pass experience, entitlements and tier redemption."""

    def __init__(self, exp_per_tier=100, tiers=None, season="s1"):
        self.exp_per_tier = exp_per_tier
        self.tiers = self._normalize_tiers(tiers or {})
        self.season = season
        self._lock = threading.RLock()
        self._accounts = {}
        self._orders = {}
        self._seasons = {season: {"started_at": None, "ended_at": None}}
        self._audit = []

    @staticmethod
    def _normalize_tiers(tiers):
        normalized = {}
        for tier, tracks in tiers.items():
            normalized[int(tier)] = {
                str(track): copy.deepcopy(reward)
                for track, reward in tracks.items()
            }
        return normalized

    @staticmethod
    def _new_account():
        return {
            "server": "default",
            "orders": [],
            "debt": 0,
            "debited": [],
            "seasons": {},
        }

    @staticmethod
    def _new_season():
        return {"exp": 0, "redeemed": [], "sources": {}}

    def _account(self, account, create=False):
        state = self._accounts.get(account)
        if state is None and create:
            state = self._new_account()
            self._accounts[account] = state
        return state

    def _season_state(self, account, season, create=False):
        account_state = self._account(account, create=create)
        if account_state is None:
            return None
        state = account_state["seasons"].get(season)
        if state is None and create:
            state = self._new_season()
            account_state["seasons"][season] = state
        return state

    def _has_premium(self, account):
        account_state = self._accounts.get(account)
        if account_state is None:
            return False
        for order_id in account_state["orders"]:
            order = self._orders.get(order_id)
            if order is not None and not order["refunded"]:
                return True
        return False

    def _record(self, action, at, **detail):
        entry = {"action": action, "at": at}
        entry.update(detail)
        self._audit.append(entry)

    def purchase(self, account, order_id, now=0, platform="pc"):
        with self._lock:
            order = self._orders.get(order_id)
            if order is not None:
                if order["account"] != account:
                    raise PassError("order_conflict")
                if order["refunded"]:
                    raise PassError("order_refunded")
                return {"status": "already_purchased", "premium": True}

            account_state = self._account(account, create=True)
            self._orders[order_id] = {
                "account": account,
                "platform": str(platform),
                "created_at": now,
                "refunded": False,
            }
            account_state["orders"].append(order_id)
            self._record(
                "purchase",
                now,
                account=account,
                order_id=order_id,
                platform=str(platform),
            )
            return {"status": "purchased", "premium": True}

    def grant(self, account, amount, source, now=0):
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise PassError("invalid_amount")

        with self._lock:
            season_state = self._season_state(account, self.season, create=True)
            meta = self._seasons.get(self.season, {})
            started_at = meta.get("started_at")
            if started_at is not None and now <= started_at:
                raise PassError("season_closed")

            if source in season_state["sources"]:
                return {
                    "status": "already_granted",
                    "granted": 0,
                    "exp": season_state["exp"],
                    "level": season_state["exp"] // self.exp_per_tier,
                    "season": self.season,
                }

            season_state["sources"][source] = amount
            season_state["exp"] += amount
            self._record(
                "grant",
                now,
                account=account,
                amount=amount,
                source=source,
                season=self.season,
            )
            return {
                "status": "granted",
                "granted": amount,
                "exp": season_state["exp"],
                "level": season_state["exp"] // self.exp_per_tier,
                "season": self.season,
            }

    def redeem(self, account, tier, track="free", now=0):
        with self._lock:
            try:
                tier = int(tier)
            except (TypeError, ValueError):
                raise PassError("unknown_tier")
            track = str(track).lower()

            if track not in ("free", "premium"):
                raise PassError("invalid_track")
            if tier not in self.tiers or track not in self.tiers[tier]:
                raise PassError("unknown_tier")

            key = "{}:{}".format(track, tier)
            season_state = self._season_state(account, self.season)
            if season_state is not None and key in season_state["redeemed"]:
                raise PassError("tier_redeemed")

            account_state = self._account(account, create=True)
            if track == "premium":
                if not self._has_premium(account):
                    raise PassError("premium_locked")
                if account_state["debt"] > 0:
                    raise PassError("reward_debt")

            exp = season_state["exp"] if season_state is not None else 0
            if tier > exp // self.exp_per_tier:
                raise PassError("tier_locked")

            season_state = self._season_state(account, self.season, create=True)
            season_state["redeemed"].append(key)
            self._record(
                "redeem",
                now,
                account=account,
                tier=tier,
                track=track,
                season=self.season,
            )
            return {
                "status": "redeemed",
                "tier": tier,
                "track": track,
                "reward": copy.deepcopy(self.tiers[tier][track]),
            }

    def reclaim(self, account, tier, track="free", now=0):
        return self.redeem(account, tier, track=track, now=now)

    def refund(self, account, order_id, now=0):
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise PassError("order_not_found")
            if order["account"] != account:
                raise PassError("order_not_owned")

            account_state = self._account(account, create=True)
            if order["refunded"]:
                return {"status": "already_refunded", "debt": account_state["debt"]}

            order["refunded"] = True
            added_debt = 0
            if not self._has_premium(account):
                for season, season_state in account_state["seasons"].items():
                    for key in season_state["redeemed"]:
                        if not key.startswith("premium:"):
                            continue
                        debited_key = "{}:{}".format(season, key)
                        if debited_key in account_state["debited"]:
                            continue
                        account_state["debited"].append(debited_key)
                        added_debt += 1
                account_state["debt"] += added_debt

            self._record(
                "refund",
                now,
                account=account,
                order_id=order_id,
                reclaimed=added_debt,
                debt=account_state["debt"],
            )
            return {
                "status": "refunded",
                "debt": account_state["debt"],
                "reclaimed": added_debt,
            }

    def season_switch(self, new_season, freeze_at, now=0):
        with self._lock:
            if new_season == self.season:
                return {
                    "status": "already_active",
                    "season": self.season,
                }
            if now < freeze_at:
                raise PassError("season_not_frozen")
            if new_season in self._seasons:
                raise PassError("season_exists")

            old_season = self.season
            old_meta = self._seasons.setdefault(
                old_season, {"started_at": None, "ended_at": None}
            )
            old_meta["ended_at"] = freeze_at
            self._seasons[new_season] = {
                "started_at": freeze_at,
                "ended_at": None,
            }
            self.season = new_season
            self._record(
                "season_switch",
                now,
                previous=old_season,
                season=new_season,
                freeze_at=freeze_at,
            )
            return {
                "status": "switched",
                "season": new_season,
                "previous": old_season,
                "freeze_at": freeze_at,
            }

    def penalty(self, account, tiers, actor, reason, now=0):
        if not isinstance(tiers, int) or isinstance(tiers, bool) or tiers < 0:
            raise PassError("invalid_tiers")
        if not isinstance(reason, str) or not reason.strip():
            raise PassError("reason_required")

        with self._lock:
            season_state = self._season_state(account, self.season, create=True)
            before = season_state["exp"]
            removed = min(before, tiers * self.exp_per_tier)
            season_state["exp"] = before - removed
            level = season_state["exp"] // self.exp_per_tier
            self._record(
                "penalty",
                now,
                account=account,
                tiers=tiers,
                actor=actor,
                reason=reason,
                season=self.season,
                exp_before=before,
                exp_after=season_state["exp"],
            )
            return {
                "status": "penalized",
                "level": level,
                "exp": season_state["exp"],
                "removed": removed,
            }

    def adjust(self, account, tiers, actor, reason, now=0):
        if not isinstance(tiers, int) or isinstance(tiers, bool):
            raise PassError("invalid_tiers")
        if not isinstance(reason, str) or not reason.strip():
            raise PassError("reason_required")

        with self._lock:
            season_state = self._season_state(account, self.season, create=True)
            before = season_state["exp"]
            season_state["exp"] = max(0, before + tiers * self.exp_per_tier)
            level = season_state["exp"] // self.exp_per_tier
            self._record(
                "adjust",
                now,
                account=account,
                tiers=tiers,
                actor=actor,
                reason=reason,
                season=self.season,
                exp_before=before,
                exp_after=season_state["exp"],
            )
            return {
                "status": "adjusted",
                "level": level,
                "exp": season_state["exp"],
            }

    def progress(self, account, season=None):
        with self._lock:
            selected = self.season if season is None else season
            account_state = self._accounts.get(account)
            season_state = (
                account_state["seasons"].get(selected)
                if account_state is not None
                else None
            )
            exp = season_state["exp"] if season_state is not None else 0
            redeemed = (
                sorted(season_state["redeemed"])
                if season_state is not None
                else []
            )
            return {
                "exp": exp,
                "level": exp // self.exp_per_tier,
                "redeemed": redeemed,
                "season": selected,
                "server": (
                    account_state["server"]
                    if account_state is not None
                    else "default"
                ),
            }

    def entitlement(self, account):
        with self._lock:
            return {"premium": self._has_premium(account)}

    def transfer(self, account, server):
        with self._lock:
            account_state = self._account(account, create=True)
            previous = account_state["server"]
            account_state["server"] = server
            self._record(
                "transfer",
                0,
                account=account,
                previous=previous,
                server=server,
            )
            return {"status": "transferred", "server": server}

    def audit(self):
        with self._lock:
            return copy.deepcopy(self._audit)

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(
                {
                    "version": 1,
                    "exp_per_tier": self.exp_per_tier,
                    "tiers": self.tiers,
                    "season": self.season,
                    "accounts": self._accounts,
                    "orders": self._orders,
                    "seasons": self._seasons,
                    "audit": self._audit,
                }
            )

    def restore(self, state):
        if not isinstance(state, dict) or "season" not in state:
            raise PassError("invalid_state")

        with self._lock:
            restored = copy.deepcopy(state)
            self.exp_per_tier = restored.get("exp_per_tier", self.exp_per_tier)
            self.tiers = self._normalize_tiers(restored.get("tiers", {}))
            self.season = restored["season"]
            self._accounts = restored.get("accounts", {})
            self._orders = restored.get("orders", {})
            self._seasons = restored.get(
                "seasons", {self.season: {"started_at": None, "ended_at": None}}
            )
            self._audit = restored.get("audit", [])

            self._seasons.setdefault(
                self.season, {"started_at": None, "ended_at": None}
            )
            for account_state in self._accounts.values():
                account_state.setdefault("server", "default")
                account_state.setdefault("orders", [])
                account_state.setdefault("debt", 0)
                account_state.setdefault("debited", [])
                account_state.setdefault("seasons", {})
                for season_state in account_state["seasons"].values():
                    season_state.setdefault("exp", 0)
                    season_state.setdefault("redeemed", [])
                    season_state.setdefault("sources", {})
            return {"status": "restored", "season": self.season}
