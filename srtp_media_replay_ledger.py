"""SRTP media replay ledger (skeleton)."""


class SrtpError(Exception):
    """Replay or key lifecycle failure raised by the ledger."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class SrtpMediaReplayLedger:
    """Tracks SRTP replay windows, rollover epochs and key generations."""

    def __init__(self, window=64, max_packets_per_key=1000):
        self.window = window
        self.max_packets_per_key = max_packets_per_key

    def open_stream(self, ssrc, source_id=None, key_id="k0", epoch=0):
        raise NotImplementedError

    def accept(self, ssrc, index, epoch=0, key_id="k0", is_control=False, tag_len=10):
        raise NotImplementedError

    def rotate_key(self, ssrc, epoch, key_id=None):
        raise NotImplementedError

    def active_epoch(self, ssrc):
        raise NotImplementedError

    def stats(self, ssrc):
        raise NotImplementedError
