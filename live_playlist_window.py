import hashlib


class PlaylistError(ValueError):
    """Raised when a live playlist operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_PART_TOLERANCE = 1e-6


class LivePlaylistWindow:
    """Monotonic low latency playlist windows and idempotent segment upload."""

    def __init__(self, window=5, target_duration=2.0, part_target=0.5):
        self.window = window
        self.target_duration = target_duration
        self.part_target = part_target
        self._streams = {}
        self._graveyard = set()
        self._segment_sequences = {}

    def _stream(self, stream):
        return self._streams.setdefault(
            stream, {"segments": {}, "order": [], "next_sequence": 1}
        )

    def _get(self, segment_id, stream):
        segment = self._stream(stream)["segments"].get(segment_id)
        if segment is None:
            raise PlaylistError("unknown_segment", segment_id)
        return segment

    @staticmethod
    def _digest(data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _uri(stream, segment_id, digest):
        return "seg://%s/%s/%s" % (stream, segment_id, digest[:16])

    def _evict(self, stream, state, keep):
        while len(state["order"]) > keep:
            evicted = state["order"].pop(0)
            self._graveyard.add((stream, evicted))

    def append(self, segment_id, data, duration=None, sequence=None,
               stream="main", complete=True):
        state = self._stream(stream)
        digest = self._digest(data)
        existing = state["segments"].get(segment_id)
        if existing is not None:
            if existing["hash"] == digest:
                return {
                    "status": "duplicate",
                    "uri": existing["uri"],
                    "sequence": existing["sequence"],
                }
            raise PlaylistError("segment_conflict", segment_id)
        if sequence is None:
            sequence = state["next_sequence"]
        elif sequence < state["next_sequence"]:
            raise PlaylistError("sequence_rolled_back", segment_id)
        known = self._segment_sequences.get(segment_id)
        if known is not None and known != sequence:
            raise PlaylistError("sequence_mismatch", segment_id)
        if duration is None:
            duration = self.target_duration
        uri = self._uri(stream, segment_id, digest)
        state["segments"][segment_id] = {
            "segment_id": segment_id,
            "hash": digest,
            "uri": uri,
            "duration": float(duration),
            "sequence": sequence,
            "complete": bool(complete),
            "published": False,
            "parts": [],
        }
        state["next_sequence"] = max(state["next_sequence"], sequence + 1)
        self._segment_sequences[segment_id] = sequence
        return {"status": "appended", "uri": uri, "sequence": sequence}

    def publish(self, segment_id, stream="main"):
        state = self._stream(stream)
        segment = state["segments"].get(segment_id)
        if segment is not None and segment["published"] \
                and segment_id in state["order"]:
            return {"status": "already_published", "uri": segment["uri"],
                    "sequence": segment["sequence"]}
        if (stream, segment_id) in self._graveyard:
            return {"status": "already_published"}
        if segment is None:
            raise PlaylistError("unknown_segment", segment_id)
        if not segment["complete"]:
            raise PlaylistError("segment_incomplete", segment_id)
        segment["published"] = True
        state["order"].append(segment_id)
        self._evict(stream, state, self.window)
        return {"status": "published", "uri": segment["uri"],
                "sequence": segment["sequence"]}

    def playlist(self, stream="main"):
        state = self._stream(stream)
        rows = []
        for segment_id in state["order"]:
            segment = state["segments"][segment_id]
            rows.append({
                "segment_id": segment_id,
                "uri": segment["uri"],
                "duration": segment["duration"],
                "sequence": segment["sequence"],
                "parts": [list(part) for part in segment["parts"]],
            })
        return rows

    def retime(self, segment_id, duration, stream="main"):
        segment = self._get(segment_id, stream)
        if segment["published"]:
            raise PlaylistError("immutable_segment", segment_id)
        segment["duration"] = float(duration)
        return {"status": "retimed", "duration": segment["duration"]}

    def resequence(self, segment_id, sequence, stream="main"):
        state = self._stream(stream)
        segment = self._get(segment_id, stream)
        if segment["published"]:
            raise PlaylistError("immutable_segment", segment_id)
        segment["sequence"] = sequence
        state["next_sequence"] = max(state["next_sequence"], sequence + 1)
        self._segment_sequences[segment_id] = sequence
        return {"status": "resequenced", "sequence": sequence}

    def add_part(self, segment_id, start, end, stream="main"):
        segment = self._get(segment_id, stream)
        if start < 0 or end <= start or end > segment["duration"]:
            raise PlaylistError("part_out_of_range", segment_id)
        if abs((end - start) - self.part_target) > _PART_TOLERANCE:
            raise PlaylistError("part_duration_mismatch", segment_id)
        segment["parts"].append([start, end])
        return {"status": "part_added", "start": start, "end": end}

    def blocking_refresh(self, since_sequence=None, now=0, wait_seconds=1.0):
        rows = self.playlist()
        if since_sequence is None:
            fresh = rows
        else:
            fresh = [row for row in rows if row["sequence"] > since_sequence]
        if fresh:
            return {"status": 200, "playlist": rows}
        return {"status": 504, "playlist": []}

    def rotate(self, keep=None):
        if keep is None:
            keep = self.window
        for stream, state in self._streams.items():
            self._evict(stream, state, keep)
        return {"status": "rotated", "keep": keep}

    def recover(self, state=None):
        self._streams = {}
        self._graveyard = set()
        self._segment_sequences = {}
        if state:
            for name, saved in state.get("streams", {}).items():
                segments = {}
                for segment_id, segment in saved.get("segments", {}).items():
                    restored = dict(segment)
                    restored["parts"] = [list(p) for p in segment.get("parts", [])]
                    segments[segment_id] = restored
                self._streams[name] = {
                    "segments": segments,
                    "order": list(saved.get("order", [])),
                    "next_sequence": saved.get("next_sequence", 1),
                }
            self._graveyard = {tuple(key) for key in state.get("graveyard", [])}
            self._segment_sequences = {
                key: int(value)
                for key, value in state.get("segment_sequences", {}).items()
            }
        return {"status": "recovered"}

    def snapshot(self):
        streams = {}
        for name, state in self._streams.items():
            segments = {}
            for segment_id, segment in state["segments"].items():
                saved = dict(segment)
                saved["parts"] = [list(p) for p in segment["parts"]]
                segments[segment_id] = saved
            streams[name] = {
                "segments": segments,
                "order": list(state["order"]),
                "next_sequence": state["next_sequence"],
            }
        return {
            "window": self.window,
            "target_duration": self.target_duration,
            "part_target": self.part_target,
            "streams": streams,
            "graveyard": sorted([list(key) for key in self._graveyard]),
            "segment_sequences": dict(self._segment_sequences),
        }
