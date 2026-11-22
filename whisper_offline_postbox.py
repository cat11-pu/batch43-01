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

    def send(self, sender, recipient, text, now=0, ttl=None, system=False):
        raise NotImplementedError

    def deliver(self, recipient, now=0):
        raise NotImplementedError

    def ack(self, recipient, message_id):
        raise NotImplementedError

    def block(self, owner, blocked):
        raise NotImplementedError

    def unblock(self, owner, blocked):
        raise NotImplementedError

    def delete_character(self, character_id):
        raise NotImplementedError

    def set_filter(self, words):
        raise NotImplementedError

    def mark_pushed(self, recipient, message_id):
        raise NotImplementedError

    def delete_session(self, owner, peer):
        raise NotImplementedError

    def queue(self, recipient):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
