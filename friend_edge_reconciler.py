class FriendError(ValueError):
    """Raised when a friend or block operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FriendEdgeReconciler:
    """Single friend edge per pair, blocking priority and merge dedupe."""

    def __init__(self, max_friends=50, request_ttl=3000):
        self.max_friends = max_friends
        self.request_ttl = request_ttl
        self._friends = {}
        self._requests = {}
        self._blocks = {}
        self._names = {}

    @staticmethod
    def _key(a, b):
        return (a, b) if a <= b else (b, a)

    def _name(self, player):
        return self._names.get(player, player)

    def _friend_count(self, player):
        return sum(1 for a, b in self._friends if player in (a, b))

    def _is_blocked(self, a, b):
        return b in self._blocks.get(a, set()) or a in self._blocks.get(b, set())

    def _drop_pair(self, key):
        self._friends.pop(key, None)
        self._requests.pop(key, None)

    def request(self, from_player, to_player, now=0):
        if from_player == to_player:
            raise FriendError("invalid_target")
        if self._is_blocked(from_player, to_player):
            raise FriendError("blocked")
        key = self._key(from_player, to_player)
        if key in self._friends:
            raise FriendError("already_friends")
        if key in self._requests:
            raise FriendError("request_pending")
        self._requests[key] = {"from": from_player, "to": to_player, "ts": now}
        return {"status": "pending"}

    def accept(self, requester, accepter, now=0):
        key = self._key(requester, accepter)
        req = self._requests.get(key)
        if req is None or req["from"] != requester or req["to"] != accepter:
            raise FriendError("unknown_request")
        if now - req["ts"] > self.request_ttl:
            del self._requests[key]
            raise FriendError("request_expired")
        if self._is_blocked(requester, accepter):
            raise FriendError("blocked")
        if (self._friend_count(requester) >= self.max_friends
                or self._friend_count(accepter) >= self.max_friends):
            raise FriendError("friend_limit")
        del self._requests[key]
        self._friends[key] = {"since": now}
        return {"status": "accepted"}

    def decline(self, requester, accepter, now=0):
        key = self._key(requester, accepter)
        req = self._requests.get(key)
        if req is None or req["from"] != requester or req["to"] != accepter:
            raise FriendError("unknown_request")
        del self._requests[key]
        return {"status": "declined"}

    def remove(self, player, friend, now=0):
        key = self._key(player, friend)
        if key not in self._friends:
            raise FriendError("not_friends")
        del self._friends[key]
        return {"status": "removed"}

    def block(self, player, target, now=0):
        if player == target:
            raise FriendError("invalid_target")
        self._blocks.setdefault(player, set()).add(target)
        self._drop_pair(self._key(player, target))
        return {"status": "blocked"}

    def unblock(self, player, target):
        self._blocks.get(player, set()).discard(target)
        return {"status": "unblocked"}

    def friends(self, player):
        rows = []
        for (a, b), edge in self._friends.items():
            if player in (a, b):
                other = b if a == player else a
                rows.append({"player": other, "name": self._name(other),
                             "since": edge["since"]})
        return rows

    def pending(self, player):
        return [{"player": req["from"], "name": self._name(req["from"]),
                 "ts": req["ts"]}
                for req in self._requests.values() if req["to"] == player]

    def blocked(self, player):
        return [{"player": target, "name": self._name(target)}
                for target in sorted(self._blocks.get(player, set()))]

    def rename(self, player, new_name):
        self._names[player] = new_name
        return {"status": "renamed"}

    def delete_character(self, player):
        for key in [k for k in self._friends if player in k]:
            del self._friends[key]
        for key in [k for k, r in self._requests.items()
                    if player in (r["from"], r["to"])]:
            del self._requests[key]
        self._blocks.pop(player, None)
        for targets in self._blocks.values():
            targets.discard(player)
        self._names.pop(player, None)
        return {"status": "deleted"}

    def merge(self, pairs, now=0):
        for a, b in pairs:
            if a == b:
                continue
            key = self._key(a, b)
            if key in self._friends or self._is_blocked(a, b):
                continue
            if (self._friend_count(a) >= self.max_friends
                    or self._friend_count(b) >= self.max_friends):
                continue
            self._requests.pop(key, None)
            self._friends[key] = {"since": now}
        return {"status": "merged"}

    def dump(self):
        return {
            "friends": [[a, b, edge["since"]]
                        for (a, b), edge in self._friends.items()],
            "requests": [dict(req) for req in self._requests.values()],
            "blocks": [[player, target]
                       for player, targets in self._blocks.items()
                       for target in sorted(targets)],
            "names": dict(self._names),
        }

    def load(self, state):
        self._friends = {}
        self._requests = {}
        self._blocks = {}
        self._names = dict(state.get("names", {}))
        for a, b, since in state.get("friends", []):
            self._friends[self._key(a, b)] = {"since": since}
        for req in state.get("requests", []):
            self._requests[self._key(req["from"], req["to"])] = {
                "from": req["from"], "to": req["to"], "ts": req["ts"]}
        for player, target in state.get("blocks", []):
            self._blocks.setdefault(player, set()).add(target)
        return {"status": "loaded"}
