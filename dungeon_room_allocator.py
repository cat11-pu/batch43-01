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
        # Ordered list of active party ids (queued or inside an instance).
        # A party keeps its slot until it leaves, so queue positions are
        # stable across allocation and requeue-on-failure.
        self._queue = []
        self._parties = {}
        self._rooms = {}
        self._room_seq = 0
        self._instance_seq = 0
        # Parties disbanded during recovery because their instance was lost.
        self.compensations = []

    def _next_room_id(self):
        self._room_seq += 1
        return "room-%d" % self._room_seq

    def _next_instance_id(self):
        self._instance_seq += 1
        return "inst-%d" % self._instance_seq

    @staticmethod
    def _progress_entry(room, difficulty, instance_id=None):
        prog = room["progress"].get(difficulty)
        if prog is None:
            prog = {"instance_id": instance_id, "bosses": [], "rewards": []}
            room["progress"][difficulty] = prog
        return prog

    def enqueue(self, party_id, size, difficulty="normal", leader=None,
                now=0):
        existing = self._parties.get(party_id)
        if existing is not None:
            if existing["status"] == "in_instance":
                raise DungeonError("ALREADY_IN_INSTANCE", party_id)
            raise DungeonError("ALREADY_QUEUED", party_id)
        self._parties[party_id] = {
            "party_id": party_id,
            "size": size,
            "difficulty": difficulty,
            "leader": leader,
            "status": "queued",
            "room_id": None,
            "enqueued_at": now,
        }
        self._queue.append(party_id)
        return {"party_id": party_id, "position": len(self._queue)}

    def queue_position(self, party_id):
        if party_id not in self._queue:
            raise DungeonError("NOT_QUEUED", party_id)
        return self._queue.index(party_id) + 1

    def allocate(self, party_ids, difficulty="normal", now=0):
        parties = []
        for pid in party_ids:
            entry = self._parties.get(pid)
            if entry is None:
                raise DungeonError("NOT_QUEUED", pid)
            if entry["status"] == "in_instance":
                raise DungeonError("ALREADY_IN_INSTANCE", pid)
            if entry["status"] != "queued":
                raise DungeonError("NOT_QUEUED", pid)
            parties.append(entry)
        for entry in parties:
            if entry["size"] < self.min_party_size:
                raise DungeonError("PARTY_TOO_SMALL", entry["party_id"])
        room_id = self._next_room_id()
        if self.room_factory is not None:
            try:
                self.room_factory(room_id)
            except Exception as exc:
                # Creation is atomic: nothing was mutated, so every party
                # keeps its original queue slot.
                raise DungeonError("ROOM_CREATE_FAILED", str(exc))
        room = {
            "room_id": room_id,
            "difficulty": difficulty,
            "status": "active",
            "parties": [p["party_id"] for p in parties],
            "progress": {},
            "created_at": now,
        }
        self._progress_entry(room, difficulty, self._next_instance_id())
        self._rooms[room_id] = room
        for entry in parties:
            entry["status"] = "in_instance"
            entry["room_id"] = room_id
        return {
            "room_id": room_id,
            "instance_id": room["progress"][difficulty]["instance_id"],
            "parties": list(room["parties"]),
        }

    def enter(self, party_id, character, room_id):
        entry = self._parties.get(party_id)
        if entry is None:
            raise DungeonError("UNKNOWN_PARTY", party_id)
        if entry["status"] != "in_instance" or entry["room_id"] != room_id:
            raise DungeonError("ROOM_MISMATCH", party_id)
        if entry["leader"] != character:
            raise DungeonError("NOT_LEADER", character)
        return {"entered": True, "party_id": party_id, "room_id": room_id}

    def leave(self, party_id):
        entry = self._parties.get(party_id)
        if entry is None:
            raise DungeonError("UNKNOWN_PARTY", party_id)
        was = entry["status"]
        if was == "in_instance":
            room = self._rooms.get(entry["room_id"])
            if room is not None and party_id in room["parties"]:
                room["parties"].remove(party_id)
        if party_id in self._queue:
            self._queue.remove(party_id)
        del self._parties[party_id]
        return {"left": True, "party_id": party_id, "was": was}

    def backfill(self, room_id, party_id, difficulty="normal"):
        room = self._rooms.get(room_id)
        if room is None:
            raise DungeonError("UNKNOWN_ROOM", room_id)
        if room["status"] == "completed":
            raise DungeonError("INSTANCE_COMPLETED", room_id)
        if room["status"] != "active":
            raise DungeonError("ROOM_NOT_ACTIVE", room_id)
        if room["difficulty"] != difficulty:
            raise DungeonError("DIFFICULTY_MISMATCH", room_id)
        entry = self._parties.get(party_id)
        if entry is None or entry["status"] != "queued":
            raise DungeonError("NOT_QUEUED", party_id)
        if len(room["parties"]) >= self.room_capacity:
            raise DungeonError("ROOM_FULL", room_id)
        room["parties"].append(party_id)
        entry["status"] = "in_instance"
        entry["room_id"] = room_id
        return {"backfilled": True, "party_id": party_id, "room_id": room_id}

    def reset_instance(self, room_id, difficulty="normal",
                       expect_instance=None):
        room = self._rooms.get(room_id)
        if room is None:
            raise DungeonError("UNKNOWN_ROOM", room_id)
        prog = self._progress_entry(room, difficulty,
                                    self._next_instance_id())
        if expect_instance is not None and expect_instance != prog["instance_id"]:
            raise DungeonError("RESET_CONFLICT", prog["instance_id"])
        prog["instance_id"] = self._next_instance_id()
        prog["bosses"] = []
        prog["rewards"] = []
        return {
            "room_id": room_id,
            "difficulty": difficulty,
            "instance_id": prog["instance_id"],
        }

    def record_boss(self, room_id, difficulty, boss):
        room = self._rooms.get(room_id)
        if room is None:
            raise DungeonError("UNKNOWN_ROOM", room_id)
        prog = self._progress_entry(room, difficulty,
                                    self._next_instance_id())
        prog["bosses"].append(boss)
        return {"recorded": boss, "room_id": room_id}

    def grant_reward(self, room_id, difficulty, reward):
        room = self._rooms.get(room_id)
        if room is None:
            raise DungeonError("UNKNOWN_ROOM", room_id)
        prog = self._progress_entry(room, difficulty,
                                    self._next_instance_id())
        prog["rewards"].append(reward)
        return {"granted": reward, "room_id": room_id}

    def complete_instance(self, room_id, difficulty="normal"):
        room = self._rooms.get(room_id)
        if room is None:
            raise DungeonError("UNKNOWN_ROOM", room_id)
        room["status"] = "completed"
        return {"completed": True, "room_id": room_id}

    def recycle_room(self, room_id):
        room = self._rooms.get(room_id)
        if room is None:
            raise DungeonError("UNKNOWN_ROOM", room_id)
        if room["parties"]:
            raise DungeonError("ROOM_OCCUPIED", room_id)
        room["progress"] = {}
        room["status"] = "free"
        return {"recycled": True, "room_id": room_id}

    def progress(self, room_id, difficulty="normal"):
        room = self._rooms.get(room_id)
        if room is None:
            return {
                "room_id": room_id,
                "difficulty": difficulty,
                "instance_id": None,
                "bosses": [],
                "rewards": [],
            }
        prog = room["progress"].get(difficulty)
        if prog is None:
            return {
                "room_id": room_id,
                "difficulty": difficulty,
                "instance_id": None,
                "bosses": [],
                "rewards": [],
            }
        return {
            "room_id": room_id,
            "difficulty": difficulty,
            "instance_id": prog["instance_id"],
            "bosses": list(prog["bosses"]),
            "rewards": list(prog["rewards"]),
        }

    def transfer_leader(self, party_id, new_leader):
        entry = self._parties.get(party_id)
        if entry is None:
            raise DungeonError("UNKNOWN_PARTY", party_id)
        entry["leader"] = new_leader
        return {"party_id": party_id, "leader": new_leader}

    def dump(self):
        return {
            "min_party_size": self.min_party_size,
            "room_capacity": self.room_capacity,
            "room_seq": self._room_seq,
            "instance_seq": self._instance_seq,
            "queue": list(self._queue),
            "parties": {pid: dict(e) for pid, e in self._parties.items()},
            "rooms": {rid: self._dump_room(r)
                      for rid, r in self._rooms.items()},
        }

    @staticmethod
    def _dump_room(room):
        return {
            "room_id": room["room_id"],
            "difficulty": room["difficulty"],
            "status": room["status"],
            "parties": list(room["parties"]),
            "created_at": room["created_at"],
            "progress": {
                diff: {
                    "instance_id": prog["instance_id"],
                    "bosses": list(prog["bosses"]),
                    "rewards": list(prog["rewards"]),
                }
                for diff, prog in room["progress"].items()
            },
        }

    def load(self, state):
        self._queue = list(state.get("queue", []))
        self._parties = {pid: dict(e)
                         for pid, e in state.get("parties", {}).items()}
        self._rooms = {}
        for rid, r in state.get("rooms", {}).items():
            room = dict(r)
            room["parties"] = list(r.get("parties", []))
            room["progress"] = {
                diff: {
                    "instance_id": prog.get("instance_id"),
                    "bosses": list(prog.get("bosses", [])),
                    "rewards": list(prog.get("rewards", [])),
                }
                for diff, prog in r.get("progress", {}).items()
            }
            self._rooms[rid] = room
        self._room_seq = state.get("room_seq", len(self._rooms))
        self._instance_seq = state.get("instance_seq", 0)
        # Recovery: a party bound to an instance that did not survive the
        # restart must not be left standing in an empty scene. Disband it
        # and record compensation instead.
        for pid, entry in self._parties.items():
            if entry.get("status") != "in_instance":
                continue
            room = self._rooms.get(entry.get("room_id"))
            if room is None or pid not in room["parties"]:
                entry["status"] = "disbanded"
                entry["room_id"] = None
                if pid in self._queue:
                    self._queue.remove(pid)
                self.compensations.append({
                    "party_id": pid,
                    "reason": "INSTANCE_LOST",
                })
        return self
