class AudioError(ValueError):
    """Raised when an audio seek or indexing request is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AudioSeekPreroll:
    """Frame indexed seeking with preroll discard and loop boundaries.

    The model keeps a per-track frame index (the decoded sample count) rather
    than estimating positions from file size, so variable bitrate files report
    accurate durations. Every seek starts decoding from a frame at or before
    the target so the decoder has the preroll it needs; the preroll samples
    are decoded but discarded, which avoids both startup clicks and missing
    beats. Each seek owns a monotonically increasing request id: any later
    seek or explicit cancel invalidates earlier requests, so a concurrent
    decoder can never feed samples from a previous position into a new one.
    """

    STALE_BUFFER_MS = 60_000

    def __init__(self, sample_rate=48000, encoder_delay=1024,
                 tail_padding=512, preroll_ms=50):
        self.sample_rate = sample_rate
        self.encoder_delay = encoder_delay
        self.tail_padding = tail_padding
        self.preroll_ms = preroll_ms
        self._tracks = {}
        self._active = {}
        self._requests = {}
        self._next_request = 1
        self._output_channels = 2
        self._paused_at = None

    def load(self, track_id, frames, sample_rate=None, vbr=False,
             size_bytes=None, bitrate=None):
        """Register a track using its decoded frame index.

        ``frames`` is the authoritative sample count taken from the container
        or frame index. ``vbr``, ``size_bytes`` and ``bitrate`` are recorded
        for diagnostics but deliberately never used to derive duration.
        """
        if frames <= 0:
            raise AudioError("invalid_track", "frame index must be positive")
        self._tracks[track_id] = {
            "frames": int(frames),
            "sample_rate": int(sample_rate or self.sample_rate),
            "vbr": bool(vbr),
            "size_bytes": size_bytes,
            "bitrate": bitrate,
            "encoder_delay": self.encoder_delay,
            "tail_padding": self.tail_padding,
        }
        self._active.pop(track_id, None)

    def _track(self, track_id):
        try:
            return self._tracks[track_id]
        except KeyError:
            raise AudioError("track_not_loaded", track_id) from None

    @staticmethod
    def _ms_to_frames(ms, sample_rate):
        return int(round(float(ms) * sample_rate / 1000.0))

    def duration_ms(self, track_id):
        """Duration from the decoded frame index, never from byte size."""
        track = self._track(track_id)
        return track["frames"] * 1000.0 / track["sample_rate"]

    def total_frames(self, track_id):
        return self._track(track_id)["frames"]

    def seek(self, track_id, target_ms, request_id=None):
        """Seek to ``target_ms`` starting from a preroll-safe frame.

        Decoding starts at least ``preroll_ms`` before the target (or at frame
        zero for the head of the track). The preroll region is reported as
        ``discard_frames`` and must be dropped before the first audible
        sample, so the first delivered sample is exactly the target frame.
        """
        track = self._track(track_id)
        sample_rate = track["sample_rate"]
        total = track["frames"]

        target_frame = self._ms_to_frames(target_ms, sample_rate)
        if target_frame < 0:
            target_frame = 0
        if target_frame >= total:
            target_frame = total - 1

        preroll = self._ms_to_frames(self.preroll_ms, sample_rate)
        start_frame = max(0, target_frame - preroll)
        discard_frames = target_frame - start_frame

        if request_id is None:
            request_id = self._next_request
            self._next_request += 1
        elif request_id in self._requests:
            raise AudioError("duplicate_request", request_id)

        previous = self._active.get(track_id)
        if previous is not None:
            self._requests.pop(previous, None)

        self._requests[request_id] = {
            "track_id": track_id,
            "cursor": target_frame,
            "end_frame": total,
            "valid": True,
        }
        self._active[track_id] = request_id

        return {
            "request_id": request_id,
            "target_frame": target_frame,
            "start_frame": start_frame,
            "discard_frames": discard_frames,
            "end_frame": total,
        }

    def read(self, request_id, count):
        """Read decoded samples for a still-active seek request.

        Samples are returned as frame indices modulo 1000 in this reference
        model. Reads against a superseded or cancelled request are rejected so
        stale decoder output can never be mixed into the current position.
        """
        request = self._requests.get(request_id)
        if request is None or not request["valid"]:
            raise AudioError("stale_request", request_id)
        if count < 0:
            raise AudioError("invalid_read", count)

        available = max(0, request["end_frame"] - request["cursor"])
        take = min(count, available)
        cursor = request["cursor"]
        request["cursor"] += take
        return [(cursor + index) % 1000 for index in range(take)]

    def cancel(self, request_id):
        """Invalidate a request; subsequent reads raise ``stale_request``."""
        request = self._requests.pop(request_id, None)
        if request is None:
            return
        request["valid"] = False
        track_id = request["track_id"]
        if self._active.get(track_id) == request_id:
            del self._active[track_id]

    def active_request(self, track_id):
        return self._active.get(track_id)

    def loop_points(self, track_id, start_ms, end_ms):
        """Snap a loop region onto decoded sample boundaries.

        Both endpoints are converted to whole samples. The loop end is clamped
        to the last audible sample, excluding encoder delay and the encoder's
        tail padding, so loop wrap never lands on inserted silence.
        """
        track = self._track(track_id)
        sample_rate = track["sample_rate"]
        audible_end = (
            track["frames"]
            - track["encoder_delay"]
            - track["tail_padding"]
        )

        start_sample = max(0, self._ms_to_frames(start_ms, sample_rate))
        end_sample = min(
            audible_end, self._ms_to_frames(end_ms, sample_rate)
        )
        if start_sample > end_sample:
            raise AudioError(
                "invalid_loop",
                {"start_sample": start_sample, "end_sample": end_sample},
            )

        return {"start_sample": start_sample, "end_sample": end_sample}

    def playlist_gap(self, first_track, second_track):
        """Return the audible gap between two gapless tracks.

        With encoder delay and tail padding trimmed on both sides, adjacent
        tracks share one continuous decoded stream, so the gap is zero.
        """
        self._track(first_track)
        self._track(second_track)
        return 0

    def set_output(self, channels):
        """Renegotiate the output whenever the channel layout changes."""
        if channels == self._output_channels:
            return {"status": "unchanged", "channels": channels}
        self._output_channels = channels
        return {"status": "renegotiated", "channels": channels}

    def output_state(self):
        return {"channels": self._output_channels}

    def pause(self, now_ms):
        self._paused_at = now_ms

    def resume(self, now_ms):
        """Resume after a pause.

        Buffered decoded data is only trusted for ``STALE_BUFFER_MS``; after
        that the pipeline must decode fresh instead of replaying stale data.
        """
        if self._paused_at is None:
            raise AudioError("not_paused")
        paused_for = now_ms - self._paused_at
        self._paused_at = None
        if paused_for > self.STALE_BUFFER_MS:
            return {"status": "redecode_required", "paused_for_ms": paused_for}
        return {"status": "resumed", "paused_for_ms": paused_for}
