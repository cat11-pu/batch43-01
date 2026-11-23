import unicodedata


class NicknameError(ValueError):
    """Raised when a nickname lease operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _normalize(nickname):
    text = unicodedata.normalize("NFKC", str(nickname))
    text = " ".join(text.split())
    return text.casefold()


def _display(nickname):
    text = unicodedata.normalize("NFKC", str(nickname))
    return " ".join(text.split())


class NicknameLeaseBook:
    """Nickname claims, renames, reservations and account restoration."""

    def __init__(self, cooldown=1000, reservation=5000):
        self.cooldown = cooldown
        self.reservation = reservation
        self._cards = {}
        self._leases = {}
        self._accounts = {}
        self._history = {}

    def _account(self, account):
        return self._accounts.setdefault(
            account, {"nickname": None, "deleted": False, "last_rename": None}
        )

    def _expire(self, norm, now):
        lease = self._leases.get(norm)
        if (
            lease is not None
            and lease["kind"] == "reserved"
            and lease["expires"] is not None
            and now >= lease["expires"]
        ):
            del self._leases[norm]

    def _spend_card(self, account):
        if self._cards.get(account, 0) <= 0:
            raise NicknameError("no_card", account)
        self._cards[account] -= 1

    def _record(self, account, old, new, at, actor):
        self._history.setdefault(account, []).append(
            {"old": old, "new": new, "at": at, "actor": actor}
        )

    def _bind(self, account, nickname):
        norm = _normalize(nickname)
        self._leases[norm] = {
            "account": account,
            "nickname": nickname,
            "kind": "active",
            "expires": None,
        }
        info = self._account(account)
        info["nickname"] = nickname
        info["deleted"] = False

    def _unbind(self, account):
        info = self._accounts.get(account)
        if info is None or info["nickname"] is None:
            return None
        old = info["nickname"]
        old_norm = _normalize(old)
        lease = self._leases.get(old_norm)
        if lease is not None and lease["account"] == account:
            del self._leases[old_norm]
        return old

    def _check_available(self, account, nickname, now):
        norm = _normalize(nickname)
        self._expire(norm, now)
        lease = self._leases.get(norm)
        if lease is None or lease["account"] == account:
            return norm
        if lease["kind"] == "reserved":
            raise NicknameError("name_reserved", nickname)
        raise NicknameError("name_taken", nickname)

    def _suffixed(self, base, now):
        index = 2
        while True:
            candidate = "{}#{}".format(base, index)
            norm = _normalize(candidate)
            self._expire(norm, now)
            if norm not in self._leases:
                return candidate
            index += 1

    def grant_card(self, account, count=1):
        self._cards[account] = self._cards.get(account, 0) + count
        return self._cards[account]

    def cards(self, account):
        return self._cards.get(account, 0)

    def seed(self, account, nickname, now=0):
        info = self._account(account)
        info["nickname"] = nickname
        info["deleted"] = False
        return {"status": "seeded", "nickname": nickname}

    def claim(self, account, nickname, now=0, card=True):
        norm = _normalize(nickname)
        self._expire(norm, now)
        lease = self._leases.get(norm)
        if lease is not None:
            if lease["account"] == account and lease["kind"] == "active":
                return {"status": "claimed", "nickname": lease["nickname"]}
            if lease["kind"] == "reserved":
                raise NicknameError("name_reserved", nickname)
            raise NicknameError("name_taken", nickname)
        if card:
            self._spend_card(account)
        self._unbind(account)
        display = _display(nickname)
        self._bind(account, display)
        return {"status": "claimed", "nickname": display}

    def rename(self, account, nickname, now=0, card=True):
        info = self._accounts.get(account)
        if info is None or info["nickname"] is None:
            raise NicknameError("no_nickname", account)
        last = info["last_rename"]
        if last is not None and now - last < self.cooldown:
            raise NicknameError("cooldown", account)
        self._check_available(account, nickname, now)
        if card:
            self._spend_card(account)
        old = self._unbind(account)
        display = _display(nickname)
        self._bind(account, display)
        info["last_rename"] = now
        self._record(account, old, display, now, actor=account)
        return {"status": "renamed", "nickname": display, "old": old}

    def release(self, account, now=0):
        info = self._accounts.get(account)
        if info is None or info["nickname"] is None:
            raise NicknameError("no_nickname", account)
        old = self._unbind(account)
        info["nickname"] = None
        return {"status": "released", "nickname": old}

    def delete_account(self, account, now=0):
        info = self._accounts.get(account)
        if info is None or info["nickname"] is None:
            raise NicknameError("no_nickname", account)
        expires = now + self.reservation
        norm = _normalize(info["nickname"])
        lease = self._leases.get(norm)
        if lease is not None and lease["account"] == account:
            lease["kind"] = "reserved"
            lease["expires"] = expires
        info["deleted"] = True
        return {
            "status": "deleted",
            "nickname": info["nickname"],
            "reserved_until": expires,
        }

    def restore(self, account, now=0):
        info = self._accounts.get(account)
        if info is None or not info["deleted"]:
            raise NicknameError("not_deleted", account)
        original = info["nickname"]
        norm = _normalize(original)
        self._expire(norm, now)
        lease = self._leases.get(norm)
        if lease is None or lease["account"] == account:
            final = original
            if lease is not None:
                del self._leases[norm]
        else:
            final = self._suffixed(original, now)
        self._bind(account, final)
        if final != original:
            self._record(account, original, final, now, actor="system")
        return {"status": "restored", "nickname": final}

    def force_rename(self, account, nickname, actor, now=0):
        info = self._accounts.get(account)
        if info is None or info["nickname"] is None:
            raise NicknameError("no_nickname", account)
        self._check_available(account, nickname, now)
        old = self._unbind(account)
        display = _display(nickname)
        self._bind(account, display)
        self._record(account, old, display, now, actor=actor)
        return {"status": "renamed", "nickname": display, "old": old, "actor": actor}

    def merge(self, accounts, now=0):
        result = {}
        taken = {}
        for account in accounts:
            info = self._account(account)
            base = info["nickname"] if info["nickname"] else str(account)
            candidate = base
            if _normalize(base) in taken:
                index = 2
                while True:
                    candidate = "{}#{}".format(base, index)
                    norm = _normalize(candidate)
                    self._expire(norm, now)
                    if norm not in taken and (
                        norm not in self._leases
                        or self._leases[norm]["account"] == account
                    ):
                        break
                    index += 1
            self._unbind(account)
            self._bind(account, candidate)
            taken[_normalize(candidate)] = account
            if candidate != base:
                self._record(account, base, candidate, now, actor="merge")
            result[account] = candidate
        return result

    def resolve(self, nickname):
        lease = self._leases.get(_normalize(nickname))
        if lease is None or lease["kind"] != "active":
            return None
        return lease["account"]

    def nickname_of(self, account):
        info = self._accounts.get(account)
        if info is None:
            return None
        return info["nickname"]

    def history(self, account):
        return [dict(entry) for entry in self._history.get(account, [])]

    def dump(self):
        return {
            "cooldown": self.cooldown,
            "reservation": self.reservation,
            "cards": dict(self._cards),
            "leases": {key: dict(lease) for key, lease in self._leases.items()},
            "accounts": {
                key: dict(info) for key, info in self._accounts.items()
            },
            "history": {
                key: [dict(entry) for entry in entries]
                for key, entries in self._history.items()
            },
        }

    def load(self, state):
        self.cooldown = state.get("cooldown", self.cooldown)
        self.reservation = state.get("reservation", self.reservation)
        self._cards = {key: int(value) for key, value in state.get("cards", {}).items()}
        self._leases = {
            key: dict(lease) for key, lease in state.get("leases", {}).items()
        }
        self._accounts = {
            key: dict(info) for key, info in state.get("accounts", {}).items()
        }
        self._history = {
            key: [dict(entry) for entry in entries]
            for key, entries in state.get("history", {}).items()
        }
        return self
