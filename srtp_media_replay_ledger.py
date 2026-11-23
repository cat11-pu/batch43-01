"""SRTP media replay ledger."""

import threading

_SEQ_MOD = 1 << 16
_SEQ_HALF = 1 << 15
_MIN_TAG_LEN = 10


class SrtpError(Exception):
    """Replay or key lifecycle failure raised by the ledger."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class _ReplayWindow:
    """Sliding replay window over 48-bit SRTP packet indices.

    Tracks the rollover counter separately from the highest sequence
    number so a wrapped packet is accepted exactly once and replays
    never advance the rollover count.
    """

    def __init__(self, size):
        self.size = size
        self.roc = 0
        self.max_seq = None
        self.highest = -1
        self.bitmask = 0
        self.rollovers = 0

    def _estimate_index(self, seq):
        if self.max_seq is None:
            return seq
        if self.max_seq - seq > _SEQ_HALF:
            return (self.roc + 1) * _SEQ_MOD + seq
        if seq - self.max_seq > _SEQ_HALF:
            return (self.roc - 1) * _SEQ_MOD + seq
        return self.roc * _SEQ_MOD + seq

    def check_and_set(self, seq):
        index = self._estimate_index(seq)
        if index < 0:
            raise SrtpError("SRTP_REPLAY", "packet predates stream start")
        if self.highest >= 0:
            delta = self.highest - index
            if delta >= self.size:
                raise SrtpError("SRTP_REPLAY", "packet older than replay window")
            if delta >= 0:
                if self.bitmask & (1 << delta):
                    raise SrtpError("SRTP_REPLAY", "duplicate packet index")
                self.bitmask |= 1 << delta
                return True
            shift = index - self.highest
            self.bitmask = ((self.bitmask << shift) | 1) if shift < self.size else 1
        else:
            self.bitmask = 1
        if self.max_seq is not None and self.max_seq - seq > _SEQ_HALF:
            self.roc += 1
            self.rollovers += 1
        self.max_seq = seq
        self.highest = index
        return True


class _StreamState:
    def __init__(self, ssrc, source_id, key_id, epoch, window):
        self.ssrc = ssrc
        self.source_id = source_id
        self.key_id = key_id
        self.epoch = epoch
        self.media = _ReplayWindow(window)
        self.control = _ReplayWindow(window)
        self.packets_under_key = 0
        self.accepted = 0
        self.rejected = 0


class SrtpMediaReplayLedger:
    """Tracks SRTP replay windows, rollover epochs and key generations."""

    def __init__(self, window=64, max_packets_per_key=1000):
        self.window = window
        self.max_packets_per_key = max_packets_per_key
        self._streams = {}
        self._lock = threading.RLock()

    def _require(self, ssrc):
        stream = self._streams.get(ssrc)
        if stream is None:
            raise SrtpError("SRTP_UNKNOWN_STREAM", "no stream for ssrc %#x" % ssrc)
        return stream

    def open_stream(self, ssrc, source_id=None, key_id="k0", epoch=0):
        with self._lock:
            existing = self._streams.get(ssrc)
            if existing is not None:
                if (
                    source_id is not None
                    and existing.source_id is not None
                    and source_id != existing.source_id
                ):
                    raise SrtpError(
                        "SRTP_SSRC_COLLISION",
                        "ssrc %#x already bound to %r" % (ssrc, existing.source_id),
                    )
                if existing.source_id is None:
                    existing.source_id = source_id
                return existing
            stream = _StreamState(ssrc, source_id, key_id, epoch, self.window)
            self._streams[ssrc] = stream
            return stream

    def accept(self, ssrc, index, epoch=0, key_id="k0", is_control=False, tag_len=10):
        with self._lock:
            stream = self._require(ssrc)
            try:
                if not 0 <= index < _SEQ_MOD:
                    raise SrtpError("SRTP_REPLAY", "sequence number out of range")
                if epoch != stream.epoch:
                    raise SrtpError("SRTP_KEY_EXPIRED", "stale key epoch")
                if key_id != stream.key_id:
                    raise SrtpError("SRTP_KEY_ID_MISMATCH", "unknown key identifier")
                if tag_len < _MIN_TAG_LEN:
                    raise SrtpError("SRTP_TAG_TOO_SHORT", "auth tag below 80 bits")
                if stream.packets_under_key >= self.max_packets_per_key:
                    raise SrtpError("SRTP_KEY_EXPIRED", "key packet limit reached")
                window = stream.control if is_control else stream.media
                window.check_and_set(index)
            except SrtpError:
                stream.rejected += 1
                raise
            stream.packets_under_key += 1
            stream.accepted += 1
            return True

    def rotate_key(self, ssrc, epoch, key_id=None):
        with self._lock:
            stream = self._require(ssrc)
            stream.epoch = epoch
            if key_id is not None:
                stream.key_id = key_id
            stream.media = _ReplayWindow(self.window)
            stream.control = _ReplayWindow(self.window)
            stream.packets_under_key = 0
            return stream.epoch

    def active_epoch(self, ssrc):
        with self._lock:
            return self._require(ssrc).epoch

    def stats(self, ssrc):
        with self._lock:
            stream = self._require(ssrc)
            return {
                "accepted": stream.accepted,
                "rejected": stream.rejected,
                "rollovers": stream.media.rollovers + stream.control.rollovers,
                "epoch": stream.epoch,
                "packets_under_key": stream.packets_under_key,
            }
