import math


class PresenterError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VideoFramePresenter:
    """Audio-master-clock video presentation queue.

    Media timestamps remain the source of truth.  The audio clock decides when
    a frame is allowed to be shown, while the current refresh rate decides on
    which display tick it is presented.  No wall clock is used for scheduling.
    """

    _EPSILON = 1e-9

    def __init__(self, refresh_hz=60.0, capacity=3, audio_latency=0.0):
        self._validate_refresh(refresh_hz)
        self._validate_finite("capacity", capacity)
        self._validate_finite("audio_latency", audio_latency)

        self.refresh_hz = float(refresh_hz)
        self.capacity = max(1, int(capacity))
        self.audio_latency = float(audio_latency)

        self._audio_clock = 0.0
        self._queue = []
        self._next_sequence = 0

        self._presented_times = []
        self._last_presentation_time = None
        self._last_presented_pts = None
        self._last_presented_frame = None

        self._paused = False
        self._frozen_frame = None
        self._pause_clock = None

        self._hdr_enabled = False
        self._display_generation = 0

        self._view_id = None
        self._views = {}

    @staticmethod
    def _validate_finite(name, value):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise PresenterError("INVALID_VALUE", f"{name} must be numeric")
        if not math.isfinite(numeric):
            raise PresenterError("INVALID_VALUE", f"{name} must be finite")
        return numeric

    def _validate_refresh(self, hz):
        value = self._validate_finite("refresh_hz", hz)
        if value <= 0.0:
            raise PresenterError("INVALID_REFRESH", "refresh_hz must be positive")
        return value

    def _validate_pts(self, pts, name="pts"):
        return self._validate_finite(name, pts)

    def _clock(self):
        if self._paused and self._pause_clock is not None:
            return self._pause_clock
        return self._audio_clock

    def _effective_clock(self):
        return self._clock() + self.audio_latency

    def _refresh_period(self):
        return 1.0 / self.refresh_hz

    def submit(self, frame_id, pts, now=None):
        """Queue a decoded frame.

        ``now`` is deliberately ignored: wall-clock time is not a presentation
        clock.  A frame whose media timestamp has already fallen behind the
        audio master is too late to show and is rejected.
        """
        del now
        pts = self._validate_pts(pts)

        if (
            self._last_presented_pts is not None
            and pts < self._last_presented_pts - self._EPSILON
        ):
            raise PresenterError(
                "LATE_FRAME",
                f"frame pts {pts} is older than presented pts {self._last_presented_pts}",
            )

        if pts < self._effective_clock() - self._EPSILON:
            raise PresenterError(
                "LATE_FRAME",
                f"frame pts {pts} is behind audio clock {self._effective_clock()}",
            )

        frame = {
            "frame_id": frame_id,
            "pts": pts,
            "sequence": self._next_sequence,
        }
        self._next_sequence += 1

        dropped = None
        if len(self._queue) >= self.capacity:
            dropped_frame = self._queue.pop(0)
            dropped = dropped_frame["frame_id"]

        self._queue.append(frame)
        self._queue.sort(key=lambda item: (item["pts"], item["sequence"]))

        return {
            "frame_id": frame_id,
            "pts": pts,
            "queued": len(self._queue),
            "dropped": dropped,
        }

    def present(self, now=None):
        """Present at most one due frame on the current refresh timeline.

        The call returns at most one frame so a burst of decoded frames cannot
        turn into a catch-up burst on screen.  ``now`` is ignored by design.
        """
        del now

        if self._paused:
            if self._frozen_frame is None:
                return self._empty_result(paused=True)
            return {
                "frame_id": self._frozen_frame["frame_id"],
                "pts": self._frozen_frame["pts"],
                "presentation_time": self._last_presentation_time,
                "paused": True,
            }

        if not self._queue:
            return self._empty_result(paused=False)

        frame = self._queue[0]
        if frame["pts"] > self._effective_clock() + self._EPSILON:
            return self._empty_result(paused=False)

        presentation_time = self._quantize_to_refresh(frame["pts"])

        self._queue.pop(0)
        self._last_presentation_time = presentation_time
        self._last_presented_pts = frame["pts"]
        self._last_presented_frame = frame
        self._presented_times.append(presentation_time)

        return {
            "frame_id": frame["frame_id"],
            "pts": frame["pts"],
            "presentation_time": presentation_time,
            "paused": False,
        }

    def _empty_result(self, paused):
        return {
            "frame_id": None,
            "pts": None,
            "presentation_time": None,
            "paused": paused,
        }

    def _quantize_to_refresh(self, pts):
        period = self._refresh_period()
        tick = math.ceil(pts / period - self._EPSILON)
        presentation_time = tick * period

        if (
            self._last_presentation_time is not None
            and presentation_time <= self._last_presentation_time + self._EPSILON
        ):
            next_tick = (
                math.floor(self._last_presentation_time / period + self._EPSILON) + 1
            )
            presentation_time = next_tick * period
            if presentation_time <= self._last_presentation_time + self._EPSILON:
                presentation_time = self._last_presentation_time + period

        return float(presentation_time)

    def set_audio_clock(self, pts):
        pts = self._validate_pts(pts)
        self._audio_clock = pts
        return {"audio_clock": pts}

    def seek(self, target_pts):
        """Atomically reset video queue and audio position to ``target_pts``."""
        target_pts = self._validate_pts(target_pts, "target_pts")

        self._audio_clock = target_pts
        self._queue.clear()
        self._paused = False
        self._frozen_frame = None
        self._pause_clock = None

        # The new media timeline starts at the seek target.  The rendered
        # timeline is still kept monotonic, including seeks backwards.
        self._last_presented_pts = None
        self._last_presented_frame = None

        return {
            "pts": target_pts,
            "audio_clock": target_pts,
            "queue": [],
            "rebuild": True,
            "refresh_hz": self.refresh_hz,
        }

    def pause(self):
        if self._paused:
            return {"paused": True, "frame_id": self._frozen_frame_id()}

        self._paused = True
        self._pause_clock = self._audio_clock
        self._frozen_frame = self._last_presented_frame
        return {"paused": True, "frame_id": self._frozen_frame_id()}

    def resume(self):
        if not self._paused:
            return {"paused": False, "frame_id": None}

        self._audio_clock = (
            self._pause_clock if self._pause_clock is not None else self._audio_clock
        )
        self._paused = False
        self._pause_clock = None
        self._frozen_frame = None

        # The frozen frame is already on screen.  It must not be presented
        # again when playback resumes.
        return {"paused": False, "frame_id": None}

    def _frozen_frame_id(self):
        if self._frozen_frame is None:
            return None
        return self._frozen_frame["frame_id"]

    def set_refresh(self, hz):
        hz = self._validate_refresh(hz)
        self.refresh_hz = hz
        self._display_generation += 1

        # Queued frames and the audio position are untouched.  The next frame
        # is quantized using the new refresh rate and the monotonic timeline
        # guard bridges the old and new cadence.
        return {
            "rebuild": True,
            "refresh_hz": hz,
            "generation": self._display_generation,
            "timeline_pts": self._last_presentation_time,
        }

    def set_hdr(self, enabled):
        enabled = bool(enabled)
        changed = enabled != self._hdr_enabled
        self._hdr_enabled = enabled
        if changed:
            self._display_generation += 1

        return {
            "rebuild": changed,
            "hdr": enabled,
            "hdr_enabled": enabled,
            "generation": self._display_generation,
            "timeline_pts": self._last_presentation_time,
        }

    def set_audio_latency(self, seconds):
        seconds = self._validate_finite("audio_latency", seconds)
        self.audio_latency = seconds
        return {
            "audio_latency": seconds,
            "audio_offset": seconds,
            "effective_audio_clock": self._effective_clock(),
        }

    def create_view(self, view_id):
        if view_id in self._views:
            return self._views[view_id]

        view = VideoFramePresenter(
            refresh_hz=self.refresh_hz,
            capacity=self.capacity,
            audio_latency=self.audio_latency,
        )
        view._view_id = view_id
        self._views[view_id] = view
        return view

    def queue(self):
        return [frame["pts"] for frame in self._queue]

    def presented(self):
        return list(self._presented_times)

    def state(self):
        return {
            "refresh_hz": self.refresh_hz,
            "capacity": self.capacity,
            "audio_latency": self.audio_latency,
            "audio_clock": self._audio_clock,
            "effective_audio_clock": self._effective_clock(),
            "queue": self.queue(),
            "presented": self.presented(),
            "paused": self._paused,
            "frozen_frame": self._frozen_frame_id(),
            "hdr": self._hdr_enabled,
            "hdr_enabled": self._hdr_enabled,
            "view_id": self._view_id,
            "last_presentation_time": self._last_presentation_time,
            "display_generation": self._display_generation,
        }

    def dump(self):
        data = self.state()
        data["frames"] = [
            {
                "frame_id": frame["frame_id"],
                "pts": frame["pts"],
                "sequence": frame["sequence"],
            }
            for frame in self._queue
        ]
        data["last_presented_pts"] = self._last_presented_pts
        data["next_sequence"] = self._next_sequence
        data["views"] = {
            view_id: view.dump() for view_id, view in self._views.items()
        }
        return data

    def load(self, state):
        """Restore a state previously produced by :meth:`dump` or :meth:`state`."""
        if not isinstance(state, dict):
            raise PresenterError("INVALID_STATE", "state must be a dictionary")

        refresh_hz = state.get("refresh_hz", self.refresh_hz)
        capacity = state.get("capacity", self.capacity)
        audio_latency = state.get("audio_latency", self.audio_latency)

        self.refresh_hz = self._validate_refresh(refresh_hz)
        self.capacity = max(1, int(self._validate_finite("capacity", capacity)))
        self.audio_latency = self._validate_finite("audio_latency", audio_latency)

        self._audio_clock = self._validate_finite(
            "audio_clock", state.get("audio_clock", 0.0)
        )
        self._paused = bool(state.get("paused", False))
        self._hdr_enabled = bool(state.get("hdr", state.get("hdr_enabled", False)))
        self._view_id = state.get("view_id", self._view_id)
        self._display_generation = int(state.get("display_generation", 0))

        frames = state.get("frames")
        if frames is None:
            frames = [
                {"frame_id": f"pts@{pts}", "pts": pts, "sequence": index}
                for index, pts in enumerate(state.get("queue", []))
            ]
        self._queue = [
            {
                "frame_id": frame.get("frame_id"),
                "pts": self._validate_pts(frame.get("pts")),
                "sequence": int(frame.get("sequence", index)),
            }
            for index, frame in enumerate(frames)
        ]
        self._queue.sort(key=lambda item: (item["pts"], item["sequence"]))
        self._next_sequence = int(state.get("next_sequence", len(self._queue)))

        self._presented_times = [
            self._validate_pts(value, "presentation_time")
            for value in state.get("presented", [])
        ]
        last_time = state.get("last_presentation_time")
        self._last_presentation_time = (
            None
            if last_time is None
            else self._validate_pts(last_time, "last_presentation_time")
        )
        if self._last_presentation_time is None and self._presented_times:
            self._last_presentation_time = self._presented_times[-1]

        last_pts = state.get("last_presented_pts")
        self._last_presented_pts = (
            None
            if last_pts is None
            else self._validate_pts(last_pts, "last_presented_pts")
        )
        self._last_presented_frame = None
        self._frozen_frame = None
        if self._paused:
            frozen_id = state.get("frozen_frame")
            if frozen_id is not None:
                self._frozen_frame = {
                    "frame_id": frozen_id,
                    "pts": self._last_presented_pts,
                }
            self._pause_clock = self._audio_clock
        else:
            self._pause_clock = None

        views = state.get("views", {})
        if isinstance(views, dict):
            self._views = {}
            for view_id, view_state in views.items():
                view = VideoFramePresenter(
                    refresh_hz=self.refresh_hz,
                    capacity=self.capacity,
                    audio_latency=self.audio_latency,
                )
                view._view_id = view_id
                view.load(view_state)
                self._views[view_id] = view

        return self
