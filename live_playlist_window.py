class PlaylistError(ValueError):
    """Raised when a live playlist operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class LivePlaylistWindow:
    """Monotonic low latency playlist windows and idempotent segment upload."""

    def __init__(self, window=5, target_duration=2.0, part_target=0.5):
        self.window = window
        self.target_duration = target_duration
        self.part_target = part_target

    def append(self, segment_id, data, duration=None, sequence=None,
               stream="main", complete=True):
        raise NotImplementedError

    def publish(self, segment_id, stream="main"):
        raise NotImplementedError

    def playlist(self, stream="main"):
        raise NotImplementedError

    def retime(self, segment_id, duration, stream="main"):
        raise NotImplementedError

    def resequence(self, segment_id, sequence, stream="main"):
        raise NotImplementedError

    def add_part(self, segment_id, start, end, stream="main"):
        raise NotImplementedError

    def blocking_refresh(self, since_sequence=None, now=0, wait_seconds=1.0):
        raise NotImplementedError

    def rotate(self, keep=None):
        raise NotImplementedError

    def recover(self, state=None):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError
