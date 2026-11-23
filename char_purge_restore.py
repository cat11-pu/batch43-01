class PurgeError(ValueError):
    """Raised when a character purge or restore operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


import copy


class CharPurgeRestore:
    """Transactional character deletion, sweeping and restoration."""

    _PURGE_STEPS = ("freeze_login", "delist_auction", "return_mail",
                    "snapshot", "finalize")

    def __init__(self, fee=100, reservation=5000, max_restored=2):
        self.fee = fee
        self.reservation = reservation
        self.max_restored = max_restored
        self._accounts = {}
        self._characters = {}
        self._pending = {}
        self._restore_counts = {}
        self._claimed_names = {}

    def _account(self, account):
        return self._accounts.setdefault(
            account, {"funds": 0, "achievements": {}})

    def fund_account(self, account, amount):
        self._account(account)["funds"] += amount
        return self._account(account)["funds"]

    def account_funds(self, account):
        return self._account(account)["funds"]

    def create_character(self, character_id, account, nickname,
                         guild_rank="member", permissions=None, inventory=None,
                         mail=None, achievements=None, now=0):
        if character_id in self._characters:
            raise PurgeError("character_exists")
        self._characters[character_id] = {
            "character_id": character_id,
            "account": account,
            "nickname": nickname,
            "guild_rank": guild_rank,
            "permissions": list(permissions or []),
            "inventory": dict(inventory or {}),
            "mail": list(mail or []),
            "achievements": dict(achievements or {}),
            "auctions": [],
            "login_frozen": False,
            "status": "active",
            "created_at": now,
            "purged_at": None,
            "snapshot": None,
        }
        acc = self._account(account)
        for key, value in (achievements or {}).items():
            acc["achievements"][key] = acc["achievements"].get(key, 0) + value
        return self._characters[character_id]

    def _apply_step(self, char, task, step, now):
        if step == "freeze_login":
            char["login_frozen"] = True
        elif step == "delist_auction":
            char["auctions"] = []
        elif step == "return_mail":
            task["held_mail"] = list(char["mail"])
        elif step == "snapshot":
            task["snapshot"] = {
                "nickname": char["nickname"],
                "guild_rank": char["guild_rank"],
                "permissions": list(char["permissions"]),
                "inventory": dict(char["inventory"]),
                "mail": list(task.get("held_mail") or char["mail"]),
                "achievements": dict(char["achievements"]),
            }
        elif step == "finalize":
            char["snapshot"] = task["snapshot"]
            char["inventory"] = {}
            char["mail"] = []
            char["auctions"] = []
            char["login_frozen"] = True
            char["status"] = "purged"
            char["purged_at"] = now

    def purge(self, character_id, actor, now=0, fail_step=None,
              crash_after=None):
        char = self._characters.get(character_id)
        if char is None:
            raise PurgeError("character_not_found")
        if char["status"] == "purging":
            raise PurgeError("character_purging")
        if char["status"] == "purged":
            raise PurgeError("character_purged")
        working = copy.deepcopy(char)
        task = {"character_id": character_id, "actor": actor,
                "started_at": now, "steps_done": [], "held_mail": [],
                "snapshot": None}
        for step in self._PURGE_STEPS:
            if fail_step == step:
                raise PurgeError("purge_failed", detail={"step": step})
            self._apply_step(working, task, step, now)
            task["steps_done"].append(step)
            if crash_after == step:
                working["status"] = "purging"
                self._characters[character_id] = working
                self._pending[character_id] = task
                return {"status": "pending", "character_id": character_id}
        self._characters[character_id] = working
        return {"status": "purged", "character_id": character_id}

    def restore(self, character_id, account, now=0):
        char = self._characters.get(character_id)
        if char is None:
            raise PurgeError("character_not_found")
        if char["account"] != account:
            raise PurgeError("not_owner")
        if char["status"] == "active":
            return {"status": "already_restored",
                    "character_id": character_id,
                    "nickname": char["nickname"],
                    "guild_rank": char["guild_rank"],
                    "permissions": list(char["permissions"])}
        if char["status"] == "purging":
            raise PurgeError("character_purging")
        if self._restore_counts.get(account, 0) >= self.max_restored:
            raise PurgeError("restore_limit")
        acc = self._account(account)
        if acc["funds"] < self.fee:
            raise PurgeError("insufficient_funds")
        acc["funds"] -= self.fee
        self._restore_counts[account] = \
            self._restore_counts.get(account, 0) + 1
        snapshot = char["snapshot"] or {}
        char["nickname"] = snapshot.get("nickname", char["nickname"])
        char["guild_rank"] = snapshot.get("guild_rank", char["guild_rank"])
        char["permissions"] = list(snapshot.get("permissions",
                                                char["permissions"]))
        char["inventory"] = dict(snapshot.get("inventory", {}))
        char["mail"] = list(snapshot.get("mail", []))
        char["login_frozen"] = False
        char["status"] = "active"
        char["purged_at"] = None
        return {"status": "restored",
                "character_id": character_id,
                "nickname": char["nickname"],
                "guild_rank": char["guild_rank"],
                "permissions": list(char["permissions"])}

    def sweep(self, now=0):
        completed = []
        for character_id in list(self._pending):
            task = self._pending.pop(character_id)
            char = self._characters.get(character_id)
            if char is None:
                continue
            for step in self._PURGE_STEPS:
                if step in task["steps_done"]:
                    continue
                self._apply_step(char, task, step, now)
                task["steps_done"].append(step)
            completed.append(character_id)
        return {"completed": completed}

    def characters(self, account=None):
        rows = []
        for char in self._characters.values():
            if char["status"] != "active":
                continue
            if account is not None and char["account"] != account:
                continue
            rows.append(self._public_row(char))
        return rows

    def _public_row(self, char):
        return {"character_id": char["character_id"],
                "account": char["account"],
                "nickname": char["nickname"],
                "guild_rank": char["guild_rank"]}

    def search(self, nickname):
        return [self._public_row(char)
                for char in self._characters.values()
                if char["status"] == "active"
                and char["nickname"] == nickname]

    def leaderboard(self):
        return [self._public_row(char)
                for char in self._characters.values()
                if char["status"] == "active"]

    def inventory(self, character_id):
        char = self._characters.get(character_id)
        if char is None:
            raise PurgeError("character_not_found")
        return dict(char["inventory"])

    def mailbox(self, character_id):
        char = self._characters.get(character_id)
        if char is None:
            raise PurgeError("character_not_found")
        return list(char["mail"])

    def transfer_item(self, from_character, to_character, item, count=1):
        src = self._characters.get(from_character)
        dst = self._characters.get(to_character)
        if src is None or dst is None:
            raise PurgeError("character_not_found")
        for char in (src, dst):
            if char["status"] == "purging":
                raise PurgeError("character_purging")
            if char["status"] == "purged":
                raise PurgeError("character_purged")
        if src["inventory"].get(item, 0) < count:
            raise PurgeError("insufficient_items")
        src["inventory"][item] -= count
        if src["inventory"][item] <= 0:
            del src["inventory"][item]
        dst["inventory"][item] = dst["inventory"].get(item, 0) + count
        return {"status": "transferred"}

    def claim_nickname(self, account, nickname, now=0):
        for char in self._characters.values():
            if char["nickname"] != nickname:
                continue
            if char["status"] == "active":
                raise PurgeError("name_taken")
            purged_at = char["purged_at"]
            if purged_at is None or now - purged_at < self.reservation:
                raise PurgeError("name_reserved")
        holder = self._claimed_names.get(nickname)
        if holder is not None and holder != account:
            raise PurgeError("name_taken")
        self._claimed_names[nickname] = account
        return {"status": "claimed", "nickname": nickname}

    def login(self, character_id, now=0):
        char = self._characters.get(character_id)
        if char is None:
            raise PurgeError("character_not_found")
        if char["status"] == "purging":
            raise PurgeError("character_purging")
        if char["status"] == "purged":
            raise PurgeError("character_purged")
        if char["login_frozen"]:
            raise PurgeError("login_frozen")
        return {"status": "ok", "character_id": character_id}

    def season_rewards(self, amount, now=0):
        paid = []
        for char in self._characters.values():
            if char["status"] != "active":
                continue
            self._account(char["account"])["funds"] += amount
            paid.append(char["character_id"])
        return paid

    def account_achievements(self, account):
        return dict(self._account(account)["achievements"])

    def dump(self):
        return copy.deepcopy({
            "accounts": self._accounts,
            "characters": self._characters,
            "pending": self._pending,
            "restore_counts": self._restore_counts,
            "claimed_names": self._claimed_names,
        })

    def load(self, state):
        state = copy.deepcopy(state)
        self._accounts = state.get("accounts", {})
        self._characters = state.get("characters", {})
        self._pending = state.get("pending", {})
        self._restore_counts = state.get("restore_counts", {})
        self._claimed_names = state.get("claimed_names", {})
        return self
