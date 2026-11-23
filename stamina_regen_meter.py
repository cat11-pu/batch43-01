import copy


class StaminaError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class StaminaRegenMeter:
    """Timestamped stamina ledger with offline regeneration.

    Stamina is kept as a per-account ledger: every mutation records the
    resulting value, the cap in effect and the settlement timestamp.
    Regeneration is derived from a monotonic clock using whole
    ``regen_seconds`` intervals, with the leftover seconds carried as a
    remainder so restarts and re-logins cannot manufacture points.
    """

    def __init__(self, cap=100, regen_seconds=300, wallet=None,
                 potion_restore=50):
        self.cap = cap
        self.regen_seconds = regen_seconds
        self.wallet = wallet
        self.potion_restore = potion_restore
        self._accounts = {}
        # Maintenance windows are stripped from the effective monotonic clock.
        self._paused_total = 0
        self._pause_start = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_now(self, now):
        """Convert wall/monotonic time into time that actually counts."""
        effective = now - self._paused_total
        if self._pause_start is not None:
            effective -= now - self._pause_start
        return effective

    def _account(self, account):
        state = self._accounts.get(account)
        if state is None:
            # Anchor a brand new ledger at the effective clock so accounts
            # first seen during a maintenance window cannot bank frozen time.
            baseline = (self._effective_now(self._pause_start)
                        if self._pause_start is not None else 0)
            state = {
                "cap": self.cap,
                "current": 0,
                "remainder": 0,
                "last_settled_at": baseline,
                "txns": {},
                "refunded": {},
                "days": {},
                "ledger": [],
            }
            self._accounts[account] = state
        return state

    def _log(self, state, at, action, **extra):
        entry = {
            "at": at,
            "action": action,
            "current": state["current"],
            "cap": state["cap"],
        }
        entry.update(extra)
        state["ledger"].append(entry)

    def _settle(self, state, now):
        """Advance regeneration up to ``now`` using whole intervals."""
        if now < state["last_settled_at"]:
            raise StaminaError(
                "CLOCK_REWIND",
                {"now": now, "last_settled_at": state["last_settled_at"]},
            )
        elapsed = now - state["last_settled_at"]
        budget = state["remainder"] + elapsed
        points, remainder = divmod(budget, self.regen_seconds)
        state["last_settled_at"] = now
        if points <= 0:
            state["remainder"] = remainder
            return 0
        room = state["cap"] - state["current"]
        gained = points if points < room else room
        state["current"] += gained
        if state["current"] >= state["cap"]:
            # No regeneration while full: the next interval starts fresh.
            state["remainder"] = 0
        else:
            state["remainder"] = remainder
        if gained:
            self._log(state, now, "regen", gained=gained)
        return gained

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def grant(self, account, amount, now=0, source="mail"):
        if amount < 0:
            raise StaminaError("INVALID_AMOUNT", {"amount": amount})
        state = self._account(account)
        effective = self._effective_now(now)
        self._settle(state, effective)
        room = state["cap"] - state["current"]
        accepted = amount if amount < room else room
        overflow = amount - accepted
        state["current"] += accepted
        self._log(state, effective, "grant", amount=accepted,
                  overflow=overflow, source=source)
        return {
            "accepted": accepted,
            "overflow": overflow,
            "current": state["current"],
            "cap": state["cap"],
        }

    def purchase(self, account, amount, now=0):
        if amount <= 0:
            raise StaminaError("INVALID_AMOUNT", {"amount": amount})
        state = self._account(account)
        effective = self._effective_now(now)
        self._settle(state, effective)
        if state["current"] + amount > state["cap"]:
            raise StaminaError(
                "CAP_EXCEEDED",
                {"current": state["current"], "amount": amount,
                 "cap": state["cap"]},
            )
        if self.wallet is None:
            raise StaminaError("WALLET_UNAVAILABLE")
        if not self.wallet(amount):
            raise StaminaError("PAYMENT_FAILED")
        state["current"] += amount
        self._log(state, effective, "purchase", amount=amount)
        return {"purchased": amount, "current": state["current"],
                "cap": state["cap"]}

    def consume(self, account, amount, txn_id, now=0, expected=None):
        if amount <= 0:
            raise StaminaError("INVALID_AMOUNT", {"amount": amount})
        state = self._account(account)
        effective = self._effective_now(now)
        self._settle(state, effective)
        if txn_id in state["txns"]:
            return {"current": state["current"], "duplicate": True,
                    "txn_id": txn_id}
        if expected is not None and expected != state["current"]:
            raise StaminaError(
                "STALE_STATE",
                {"expected": expected, "current": state["current"]},
            )
        if state["current"] < amount:
            raise StaminaError(
                "INSUFFICIENT_STAMINA",
                {"current": state["current"], "required": amount},
            )
        state["current"] -= amount
        state["txns"][txn_id] = amount
        self._log(state, effective, "consume", amount=amount,
                  txn_id=txn_id)
        return {"current": state["current"], "duplicate": False,
                "txn_id": txn_id}

    def refund(self, account, txn_id, now=0):
        state = self._account(account)
        effective = self._effective_now(now)
        self._settle(state, effective)
        amount = state["txns"].get(txn_id)
        if amount is None:
            raise StaminaError("UNKNOWN_TXN", {"txn_id": txn_id})
        if txn_id in state["refunded"]:
            return {"refunded": 0, "duplicate": True,
                    "current": state["current"], "txn_id": txn_id}
        room = state["cap"] - state["current"]
        restored = amount if amount < room else room
        overflow = amount - restored
        state["current"] += restored
        state["refunded"][txn_id] = amount
        self._log(state, effective, "refund", amount=restored,
                  overflow=overflow, txn_id=txn_id)
        return {"refunded": restored, "overflow": overflow,
                "duplicate": False, "current": state["current"],
                "txn_id": txn_id}

    def tick(self, account, now=0):
        state = self._account(account)
        effective = self._effective_now(now)
        if self._pause_start is not None:
            # Maintenance freezes the meter: no settling and no moving of the
            # next-regen anchor while paused.
            return {
                "current": state["current"],
                "cap": state["cap"],
                "remainder": state["remainder"],
                "settled_at": state["last_settled_at"],
            }
        self._settle(state, effective)
        return {
            "current": state["current"],
            "cap": state["cap"],
            "remainder": state["remainder"],
            "settled_at": state["last_settled_at"],
        }

    def set_cap(self, account, new_cap, mode="fill_delta", now=0):
        if new_cap <= 0:
            raise StaminaError("INVALID_CAP", {"cap": new_cap})
        state = self._account(account)
        effective = self._effective_now(now)
        self._settle(state, effective)
        old_cap = state["cap"]
        if mode == "fill_delta":
            delta = new_cap - old_cap
            if delta > 0:
                state["current"] = min(new_cap,
                                        state["current"] + delta)
            else:
                state["current"] = min(state["current"], new_cap)
        elif mode == "reset":
            state["current"] = new_cap
            state["remainder"] = 0
        else:
            raise StaminaError("UNKNOWN_CAP_MODE", {"mode": mode})
        state["cap"] = new_cap
        state["last_settled_at"] = effective
        self._log(state, effective, "set_cap", mode=mode,
                  old_cap=old_cap)
        return {"current": state["current"], "cap": state["cap"]}

    def set_maintenance(self, paused, now=0):
        if paused:
            if self._pause_start is None:
                self._pause_start = now
        else:
            if self._pause_start is not None:
                if now < self._pause_start:
                    raise StaminaError("CLOCK_REWIND",
                                       {"now": now,
                                        "pause_start": self._pause_start})
                self._paused_total += now - self._pause_start
                self._pause_start = None
        return {"paused": self._pause_start is not None,
                "paused_total": self._paused_total}

    def daily_bonus(self, account, day, amount=10, now=0):
        if amount <= 0:
            raise StaminaError("INVALID_AMOUNT", {"amount": amount})
        state = self._account(account)
        effective = self._effective_now(now)
        self._settle(state, effective)
        day_key = str(day)
        if day_key in state["days"]:
            return {"granted": 0, "already_claimed": True,
                    "current": state["current"], "day": day_key}
        room = state["cap"] - state["current"]
        accepted = amount if amount < room else room
        overflow = amount - accepted
        state["current"] += accepted
        state["days"][day_key] = True
        self._log(state, effective, "daily_bonus", amount=accepted,
                  overflow=overflow, day=day_key)
        return {"granted": accepted, "overflow": overflow,
                "already_claimed": False, "current": state["current"],
                "day": day_key}

    def use_potion(self, account, potions, now=0):
        if potions < 0:
            raise StaminaError("INVALID_AMOUNT", {"potions": potions})
        state = self._account(account)
        effective = self._effective_now(now)
        self._settle(state, effective)
        if state["current"] >= state["cap"]:
            return {"used": 0, "potions_left": potions,
                    "reason": "AT_CAP", "current": state["current"]}
        used = 0
        remaining = potions
        while remaining > 0 and state["current"] < state["cap"]:
            room = state["cap"] - state["current"]
            state["current"] += min(self.potion_restore, room)
            used += 1
            remaining -= 1
        self._log(state, effective, "potion", used=used)
        return {"used": used, "potions_left": remaining,
                "reason": "OK", "current": state["current"]}

    def snapshot(self, account):
        state = self._account(account)
        return {
            "current": state["current"],
            "cap": state["cap"],
            "remainder": state["remainder"],
            "last_settled_at": state["last_settled_at"],
        }

    def dump(self):
        return {
            "config": {
                "cap": self.cap,
                "regen_seconds": self.regen_seconds,
                "potion_restore": self.potion_restore,
            },
            "paused_total": self._paused_total,
            "pause_start": self._pause_start,
            "accounts": copy.deepcopy(self._accounts),
        }

    def load(self, state):
        config = state.get("config", {})
        self.cap = config.get("cap", self.cap)
        self.regen_seconds = config.get("regen_seconds",
                                        self.regen_seconds)
        self.potion_restore = config.get("potion_restore",
                                         self.potion_restore)
        self._paused_total = state.get("paused_total", 0)
        self._pause_start = state.get("pause_start")
        loaded_accounts = state.get("accounts", {})
        self._accounts = copy.deepcopy(loaded_accounts)
        for loaded in self._accounts.values():
            loaded.setdefault("cap", self.cap)
            loaded.setdefault("current", 0)
            loaded.setdefault("remainder", 0)
            loaded.setdefault("last_settled_at", 0)
            loaded.setdefault("txns", {})
            loaded.setdefault("refunded", {})
            loaded.setdefault("days", {})
            loaded.setdefault("ledger", [])
        return self
