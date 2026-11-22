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

    def request(self, from_player, to_player, now=0):
        raise NotImplementedError

    def accept(self, requester, accepter, now=0):
        raise NotImplementedError

    def decline(self, requester, accepter, now=0):
        raise NotImplementedError

    def remove(self, player, friend, now=0):
        raise NotImplementedError

    def block(self, player, target, now=0):
        raise NotImplementedError

    def unblock(self, player, target):
        raise NotImplementedError

    def friends(self, player):
        raise NotImplementedError

    def pending(self, player):
        raise NotImplementedError

    def blocked(self, player):
        raise NotImplementedError

    def rename(self, player, new_name):
        raise NotImplementedError

    def delete_character(self, player):
        raise NotImplementedError

    def merge(self, pairs, now=0):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
