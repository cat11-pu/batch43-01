class DungeonError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class DungeonRoomAllocator:
    """Party queue, room allocation, progress and recycling."""

    def __init__(self, min_party_size=5, room_capacity=5, room_factory=None):
        self.min_party_size = min_party_size
        self.room_capacity = room_capacity
        self.room_factory = room_factory

    def enqueue(self, party_id, size, difficulty="normal", leader=None,
                now=0):
        raise NotImplementedError

    def queue_position(self, party_id):
        raise NotImplementedError

    def allocate(self, party_ids, difficulty="normal", now=0):
        raise NotImplementedError

    def enter(self, party_id, character, room_id):
        raise NotImplementedError

    def leave(self, party_id):
        raise NotImplementedError

    def backfill(self, room_id, party_id, difficulty="normal"):
        raise NotImplementedError

    def reset_instance(self, room_id, difficulty="normal",
                       expect_instance=None):
        raise NotImplementedError

    def record_boss(self, room_id, difficulty, boss):
        raise NotImplementedError

    def grant_reward(self, room_id, difficulty, reward):
        raise NotImplementedError

    def complete_instance(self, room_id, difficulty="normal"):
        raise NotImplementedError

    def recycle_room(self, room_id):
        raise NotImplementedError

    def progress(self, room_id, difficulty="normal"):
        raise NotImplementedError

    def transfer_leader(self, party_id, new_leader):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
