"""CAEP shared-signal event journal (skeleton)."""


class CaepError(Exception):
    """Configuration failure raised by the journal."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class CaepEventJournal:
    """Applies CAEP events exactly once and keeps revocation tombstones."""

    def __init__(self, issuer="issuer-a", state_path=None, tombstone_ttl=3600.0):
        self.issuer = issuer
        self.state_path = state_path
        self.tombstone_ttl = tombstone_ttl

    def ingest(self, event, now=0.0):
        raise NotImplementedError

    def is_active(self, session_id, now=0.0):
        raise NotImplementedError

    def journal(self):
        raise NotImplementedError

    def tombstones(self):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
