class PostboxError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class WhisperOfflinePostbox:
    """Per-conversation offline message queue with ack and filtering."""

    def __init__(self, queue_limit=50, default_ttl=3600):
        self.queue_limit = queue_limit
        self.default_ttl = default_ttl
        self._queues = {}
        self._system_queues = {}
        self._blocked = {}
        self._filter_words = set()
        self._deleted_characters = set()
        self._acked_ids = set()
        self._counter = 0

    def _next_id(self):
        self._counter += 1
        return "m%d" % self._counter

    def _is_blocked(self, recipient, sender):
        return sender in self._blocked.get(recipient, set())

    def _is_filtered(self, text):
        return any(word in text for word in self._filter_words)

    def _view(self, message):
        return {
            "id": message["id"],
            "sender": message["sender"],
            "recipient": message["recipient"],
            "text": message["text"],
            "sent_at": message["sent_at"],
            "expires_at": message["expires_at"],
            "system": message["system"],
            "reply_allowed": message["reply_allowed"],
        }

    def send(self, sender, recipient, text, now=0, ttl=None, system=False):
        if self._is_blocked(recipient, sender):
            raise PostboxError("BLOCKED", detail={"sender": sender,
                                                  "recipient": recipient})
        if self._is_filtered(text):
            raise PostboxError("FILTERED", detail={"text": text})
        queue = self._system_queues if system else self._queues
        pending = queue.setdefault(recipient, [])
        if not system and len(pending) >= self.queue_limit:
            raise PostboxError("QUEUE_FULL", detail={"recipient": recipient})
        lifetime = self.default_ttl if ttl is None else ttl
        message = {
            "id": self._next_id(),
            "sender": sender,
            "recipient": recipient,
            "text": text,
            "sent_at": now,
            "expires_at": now + lifetime,
            "system": system,
            "reply_allowed": sender not in self._deleted_characters,
            "pushed": False,
        }
        pending.append(message)
        return {"message_id": message["id"],
                "expires_at": message["expires_at"]}

    def _sweep(self, recipient, now):
        for store in (self._system_queues, self._queues):
            kept = []
            for message in store.get(recipient, []):
                expired = now > message["expires_at"]
                if expired or self._is_filtered(message["text"]) or \
                        self._is_blocked(recipient, message["sender"]):
                    continue
                kept.append(message)
            store[recipient] = kept

    def deliver(self, recipient, now=0):
        self._sweep(recipient, now)
        ready = []
        for store in (self._system_queues, self._queues):
            for message in store.get(recipient, []):
                if not message["pushed"]:
                    ready.append(self._view(message))
        return ready

    def ack(self, recipient, message_id):
        if message_id in self._acked_ids:
            return {"acked": False, "duplicate": True}
        for store in (self._system_queues, self._queues):
            pending = store.get(recipient, [])
            for index, message in enumerate(pending):
                if message["id"] == message_id:
                    del pending[index]
                    self._acked_ids.add(message_id)
                    return {"acked": True}
        return {"acked": False}

    def block(self, owner, blocked):
        self._blocked.setdefault(owner, set()).add(blocked)

    def unblock(self, owner, blocked):
        self._blocked.get(owner, set()).discard(blocked)

    def delete_character(self, character_id):
        self._deleted_characters.add(character_id)
        for store in (self._system_queues, self._queues):
            for pending in store.values():
                for message in pending:
                    if message["sender"] == character_id:
                        message["reply_allowed"] = False

    def set_filter(self, words):
        self._filter_words = set(words)

    def mark_pushed(self, recipient, message_id):
        for store in (self._system_queues, self._queues):
            for message in store.get(recipient, []):
                if message["id"] == message_id:
                    message["pushed"] = True
                    return

    def delete_session(self, owner, peer):
        for store in (self._system_queues, self._queues):
            store[owner] = [m for m in store.get(owner, [])
                            if m["sender"] != peer]
            store[peer] = [m for m in store.get(peer, [])
                           if m["sender"] != owner]

    def queue(self, recipient):
        pending = self._system_queues.get(recipient, []) + \
            self._queues.get(recipient, [])
        return [self._view(message) for message in pending]

    def dump(self):
        messages = []
        for store in (self._system_queues, self._queues):
            for pending in store.values():
                messages.extend(dict(message) for message in pending)
        messages.sort(key=lambda message: int(message["id"][1:]))
        return {
            "counter": self._counter,
            "messages": messages,
            "blocked": {owner: sorted(peers)
                        for owner, peers in self._blocked.items()},
            "filter_words": sorted(self._filter_words),
            "deleted_characters": sorted(self._deleted_characters),
            "acked_ids": sorted(self._acked_ids),
        }

    def load(self, state):
        self._counter = state.get("counter", 0)
        self._blocked = {owner: set(peers)
                         for owner, peers in
                         state.get("blocked", {}).items()}
        self._filter_words = set(state.get("filter_words", []))
        self._deleted_characters = set(state.get("deleted_characters", []))
        self._acked_ids = set(state.get("acked_ids", []))
        self._queues = {}
        self._system_queues = {}
        for message in state.get("messages", []):
            store = self._system_queues if message["system"] else self._queues
            store.setdefault(message["recipient"], []).append(dict(message))
