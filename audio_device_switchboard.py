from fractions import Fraction


class AudioError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _channel_map(channels):
    """Return the mixer-bus placement for a device channel layout."""
    if channels < 1:
        raise AudioError("INVALID_FORMAT", {"channels": channels})

    layout = {
        "front_left": 0,
        "front_right": 0 if channels == 1 else 1,
        "voice": 0,
    }
    if channels >= 3:
        layout["center"] = 2
        layout["voice"] = 2
    if channels >= 4:
        layout["lfe"] = 3
    if channels >= 6:
        layout["back_left"] = 4
        layout["back_right"] = 5
    if channels >= 8:
        layout["side_left"] = 6
        layout["side_right"] = 7
    return layout


class _Device:
    __slots__ = (
        "device_id",
        "sample_rate",
        "channels",
        "latency",
        "healthy",
        "exclusive",
        "present",
        "released",
        "draining",
        "initialized",
        "generation",
        "voices",
        "in_flight",
        "refcount",
        "buffered_frames",
        "channel_map",
        "callback_view",
    )

    def __init__(
        self,
        device_id,
        sample_rate,
        channels,
        latency=0.0,
        healthy=True,
        exclusive=False,
        voices=None,
    ):
        self.device_id = device_id
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.latency = float(latency)
        self.healthy = bool(healthy)
        self.exclusive = bool(exclusive)
        self.present = True
        self.released = False
        self.draining = False
        self.initialized = bool(healthy)
        self.generation = 0
        self.voices = set(voices or ())
        self.in_flight = 0
        self.refcount = 1
        self.buffered_frames = 0
        self.channel_map = _channel_map(self.channels)
        self.callback_view = None


class AudioDeviceSwitchboard:
    """Audio output device list, format migration and voices.

    The playhead is kept as a time position. A format change therefore does
    not move already played content backwards, even when the target device has
    a lower sample rate.
    """

    def __init__(self, sample_rate=48000, channels=2, mixer=None):
        try:
            sample_rate = int(sample_rate)
            channels = int(channels)
        except (TypeError, ValueError) as exc:
            raise AudioError(
                "INVALID_FORMAT",
                {"sample_rate": sample_rate, "channels": channels},
            ) from exc
        if sample_rate <= 0:
            raise AudioError("INVALID_FORMAT", {"sample_rate": sample_rate})
        if channels <= 0:
            raise AudioError("INVALID_FORMAT", {"channels": channels})

        self.engine_sample_rate = sample_rate
        self.engine_channels = channels
        self.sample_rate = self.engine_sample_rate
        self.channels = self.engine_channels
        self.mixer = mixer

        self._devices = {}
        self._device_order = []
        self._current = None
        self._playhead = Fraction(0, 1)
        self._played_samples = Fraction(0, 1)
        self._format_generation = 0
        self._bus_generation = 0
        self._resampler_epoch = 0
        self._resampler_state = None
        self._bus_channel_map = _channel_map(self.engine_channels)
        self._latency = 0.0
        self._compensation_samples = 0
        self._recovery = None
        self._switch_serial = 0
        self._null_view = self._make_null_view()

    def add_device(
        self,
        device_id,
        sample_rate=None,
        channels=None,
        latency=0.0,
        healthy=True,
        exclusive=False,
    ):
        device_rate = self.engine_sample_rate if sample_rate is None else sample_rate
        device_channels = self.engine_channels if channels is None else channels
        try:
            device_rate = int(device_rate)
            device_channels = int(device_channels)
            latency = float(latency)
            _channel_map(device_channels)
        except (TypeError, ValueError) as exc:
            raise AudioError("INVALID_FORMAT", {"device": device_id}) from exc
        if device_rate <= 0 or device_channels <= 0 or latency < 0.0:
            raise AudioError(
                "INVALID_FORMAT",
                {
                    "device": device_id,
                    "sample_rate": device_rate,
                    "channels": device_channels,
                    "latency": latency,
                },
            )

        device = self._devices.get(device_id)
        if device is None:
            device = _Device(
                device_id,
                device_rate,
                device_channels,
                latency=latency,
                healthy=healthy,
                exclusive=exclusive,
            )
            self._devices[device_id] = device
            self._device_order.append(device_id)
            self._refresh_device_view(device)
            if not device.healthy:
                self._recovery = {
                    "device": device_id,
                    "reason": "initialization_failed",
                }
            return {
                "added": True,
                "device": device_id,
                "healthy": device.healthy,
                "generation": device.generation,
            }

        was_present = device.present
        old_rate = device.sample_rate
        old_channels = device.channels
        old_latency = device.latency
        format_changed = (
            old_rate != device_rate or old_channels != device_channels
        )

        device.sample_rate = device_rate
        device.channels = device_channels
        device.latency = latency
        device.healthy = bool(healthy)
        device.exclusive = bool(exclusive)
        device.present = True
        device.released = False
        device.draining = False
        device.initialized = bool(healthy)
        device.channel_map = _channel_map(device_channels)
        device.generation += 1
        self._refresh_device_view(device)

        if not device.healthy:
            self._recovery = {
                "device": device_id,
                "reason": "initialization_failed",
            }

        if device_id == self._current and not device.healthy:
            self._fallback_to_null(
                reason="initialization_failed",
                requested=device_id,
            )
        elif device_id == self._current and format_changed:
            self._apply_bus_format(device_rate, device_channels)
            self._refresh_all_views()

        if device.healthy:
            self._clear_recovery(device_id)

        return {
            "added": not was_present,
            "device": device_id,
            "duplicate": was_present,
            "reused": True,
            "healthy": device.healthy,
            "format_changed": format_changed,
            "latency_changed": old_latency != latency,
            "generation": device.generation,
            "voice_count": len(device.voices),
        }

    def remove_device(self, device_id):
        device = self._devices.get(device_id)
        if device is None:
            return {"removed": False, "device": device_id, "reason": "not_found"}

        was_current = self._current == device_id
        device.present = False
        device.released = True
        device.draining = False
        device.generation += 1
        self._refresh_device_view(device)

        if was_current:
            self._current = None
            self._apply_bus_format(
                self.engine_sample_rate,
                self.engine_channels,
            )
            self._refresh_all_views()

        return {
            "removed": True,
            "device": device_id,
            "was_current": was_current,
            "fallback": "null" if was_current else self._current_label(),
            "voice_count": len(device.voices),
        }

    def drain(self, device_id):
        device = self._devices.get(device_id)
        if device is None:
            return {"drained": False, "device": device_id, "reason": "not_found"}

        buffered = device.buffered_frames
        device.buffered_frames = 0
        was_current = self._current == device_id
        if was_current:
            self._refresh_device_view(device)
            return {
                "drained": True,
                "device": device_id,
                "buffered_frames": buffered,
                "released": False,
            }

        device.draining = False
        device.released = True
        device.generation += 1
        self._refresh_device_view(device)
        return {
            "drained": True,
            "device": device_id,
            "buffered_frames": buffered,
            "released": True,
        }

    def set_default(self, device_id):
        if device_id == "null":
            previous = self._current_label()
            changed = self._current is not None
            self._fallback_to_null(reason="explicit")
            return {
                "switched": changed,
                "device": "null",
                "previous": previous,
            }

        device = self._devices.get(device_id)
        if device is None:
            result = self._fallback_to_null(
                reason="device_not_found",
                requested=device_id,
            )
            result["switched"] = False
            result["requested"] = device_id
            return result

        if not device.present:
            result = self._fallback_to_null(
                reason="device_removed",
                requested=device_id,
            )
            result["switched"] = False
            result["requested"] = device_id
            return result

        if not device.healthy or not device.initialized:
            result = self._fallback_to_null(
                reason="initialization_failed",
                requested=device_id,
            )
            result["switched"] = False
            result["requested"] = device_id
            return result

        if self._current == device_id and not device.released:
            return {
                "switched": False,
                "device": device_id,
                "previous": device_id,
                "sample_rate": device.sample_rate,
                "channels": device.channels,
                "reason": "already_current",
            }

        try:
            target_channel_map = _channel_map(device.channels)
            target_rate = int(device.sample_rate)
            target_channels = int(device.channels)
            if target_rate <= 0:
                raise AudioError("INVALID_FORMAT")
        except (AudioError, TypeError, ValueError):
            result = self._fallback_to_null(
                reason="initialization_failed",
                requested=device_id,
            )
            result["switched"] = False
            result["requested"] = device_id
            return result

        previous = self._current
        previous_device = self._devices.get(previous) if previous is not None else None
        if previous_device is not None and previous != device_id:
            previous_device.draining = previous_device.present

        device.released = False
        device.draining = False
        device.initialized = True
        device.channel_map = target_channel_map
        device.generation += 1
        self._current = device_id
        bus_info = self._apply_bus_format(target_rate, target_channels)
        self._recovery = None
        self._switch_serial += 1
        self._refresh_all_views()

        return {
            "switched": True,
            "device": device_id,
            "previous": previous if previous is not None else "null",
            "sample_rate": target_rate,
            "channels": target_channels,
            "channel_map": target_channel_map,
            "generation": device.generation,
            "switch_serial": self._switch_serial,
            "playhead": float(self._playhead),
            "played_samples": int(self._played_samples),
            **bus_info,
        }

    def current(self):
        return self._current_label()

    def active_devices(self):
        return [
            device_id
            for device_id in self._device_order
            if self._is_active(self._devices[device_id])
        ]

    def format(self, device_id=None):
        if device_id is None:
            if self._current is None:
                return self._null_format()
            device = self._devices[self._current]
            return self._device_format(device)

        if device_id == "null":
            return self._null_format()

        device = self._devices.get(device_id)
        if device is None:
            raise AudioError("DEVICE_NOT_FOUND", {"device": device_id})
        if not device.present:
            raise AudioError("DEVICE_RELEASED", {"device": device_id})
        return self._device_format(device)

    def playhead(self):
        return float(self._playhead)

    def played_samples(self):
        """Monotonic sample position expressed at the engine sample rate."""
        return int(self._played_samples)

    def sample_position(self):
        return self.played_samples()

    def advance(self, samples):
        try:
            samples = int(samples)
        except (TypeError, ValueError) as exc:
            raise AudioError("INVALID_SAMPLE_COUNT", {"samples": samples}) from exc
        if samples < 0:
            raise AudioError("INVALID_SAMPLE_COUNT", {"samples": samples})

        rate = self.sample_rate if self.sample_rate > 0 else self.engine_sample_rate
        self._playhead += Fraction(samples, rate)
        self._played_samples += Fraction(
            samples * self.engine_sample_rate,
            rate,
        )
        self._update_playback_views()
        return {
            "samples": samples,
            "playhead": float(self._playhead),
            "played_samples": int(self._played_samples),
            "sample_rate": rate,
        }

    def callback(self, device_id):
        if device_id is None:
            device_id = self._current

        if device_id == "null":
            if self._current is not None:
                raise AudioError("DEVICE_RELEASED", {"device": "null"})
            return self._null_view

        device = self._devices.get(device_id)
        if device is None:
            raise AudioError("DEVICE_NOT_FOUND", {"device": device_id})
        if not device.present or device.released:
            raise AudioError("DEVICE_RELEASED", {"device": device_id})
        if not device.healthy or not device.initialized:
            raise AudioError("DEVICE_UNHEALTHY", {"device": device_id})
        if self._current != device_id and not device.draining:
            raise AudioError("DEVICE_RELEASED", {"device": device_id})

        device.in_flight += 1
        device.refcount = (
            1 if device.present and not device.released else 0
        ) + device.in_flight
        try:
            return device.callback_view
        finally:
            device.in_flight -= 1
            device.refcount = (
                1 if device.present and not device.released else 0
            ) + device.in_flight

    def add_voice(self, device_id, voice_id):
        device = self._devices.get(device_id)
        if device is None:
            raise AudioError("DEVICE_NOT_FOUND", {"device": device_id})
        if not device.present:
            raise AudioError("DEVICE_RELEASED", {"device": device_id})

        added = voice_id not in device.voices
        if added:
            device.voices.add(voice_id)
        return {
            "added": added,
            "device": device_id,
            "voice": voice_id,
            "voice_id": voice_id,
            "count": len(device.voices),
        }

    def voice_count(self, device_id):
        device = self._devices.get(device_id)
        return len(device.voices) if device is not None else 0

    def set_audio_latency(self, seconds):
        try:
            seconds = float(seconds)
        except (TypeError, ValueError) as exc:
            raise AudioError("INVALID_LATENCY", {"seconds": seconds}) from exc
        if seconds < 0.0:
            raise AudioError("INVALID_LATENCY", {"seconds": seconds})

        self._latency = seconds
        rate = self.sample_rate if self.sample_rate > 0 else self.engine_sample_rate
        self._compensation_samples = int(round(seconds * rate))
        self._refresh_all_views()
        notified = self._notify_mixer_latency()
        return {
            "compensation_samples": self._compensation_samples,
            "compensation": self._compensation_samples,
            "sample_rate": rate,
            "latency": seconds,
            "mixer_notified": notified,
        }

    def lose_exclusive(self):
        device = self._devices.get(self._current) if self._current is not None else None
        if device is None:
            return {
                "continued": True,
                "device": "null",
                "exclusive": False,
                "fallback": True,
                "playhead": float(self._playhead),
            }

        device.exclusive = False
        device.generation += 1
        if not device.present or not device.healthy or not device.initialized:
            self._fallback_to_null(
                reason="exclusive_lost",
                requested=device.device_id,
            )
            return {
                "continued": True,
                "device": "null",
                "exclusive": False,
                "fallback": True,
                "playhead": float(self._playhead),
            }

        self._refresh_device_view(device)
        return {
            "continued": True,
            "device": device.device_id,
            "exclusive": False,
            "fallback": False,
            "playhead": float(self._playhead),
        }

    def dump(self):
        devices = {}
        for device_id in self._device_order:
            device = self._devices[device_id]
            devices[device_id] = {
                "device_id": device.device_id,
                "sample_rate": device.sample_rate,
                "channels": device.channels,
                "latency": device.latency,
                "healthy": device.healthy,
                "exclusive": device.exclusive,
                "present": device.present,
                "released": device.released,
                "draining": device.draining,
                "initialized": device.initialized,
                "generation": device.generation,
                "voices": sorted(device.voices, key=str),
                "voice_ids": sorted(device.voices, key=str),
                "buffered_frames": device.buffered_frames,
                "refcount": device.refcount,
                "in_flight": device.in_flight,
                "channel_map": dict(device.channel_map),
            }

        return {
            "engine_sample_rate": self.engine_sample_rate,
            "engine_channels": self.engine_channels,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "current": self._current_label(),
            "playhead": float(self._playhead),
            "playhead_seconds": float(self._playhead),
            "playhead_fraction": [
                self._playhead.numerator,
                self._playhead.denominator,
            ],
            "played_samples": int(self._played_samples),
            "played_samples_fraction": [
                self._played_samples.numerator,
                self._played_samples.denominator,
            ],
            "format_generation": self._format_generation,
            "bus_generation": self._bus_generation,
            "resampler_epoch": self._resampler_epoch,
            "latency": self._latency,
            "compensation_samples": self._compensation_samples,
            "recovery": dict(self._recovery) if self._recovery else None,
            "switch_serial": self._switch_serial,
            "devices": devices,
            "device_order": list(self._device_order),
            "active_devices": self.active_devices(),
        }

    def load(self, state):
        if not isinstance(state, dict):
            raise AudioError("INVALID_STATE", {"state_type": type(state).__name__})

        engine_rate = state.get(
            "engine_sample_rate",
            state.get("sample_rate", self.engine_sample_rate),
        )
        engine_channels = state.get(
            "engine_channels",
            state.get("channels", self.engine_channels),
        )
        try:
            engine_rate = int(engine_rate)
            engine_channels = int(engine_channels)
            _channel_map(engine_channels)
        except (TypeError, ValueError, AudioError) as exc:
            raise AudioError("INVALID_STATE", {"reason": "bad_engine_format"}) from exc
        if engine_rate <= 0:
            raise AudioError("INVALID_STATE", {"reason": "bad_engine_format"})

        raw_devices = state.get("devices", {})
        if isinstance(raw_devices, dict):
            device_items = list(raw_devices.items())
        elif isinstance(raw_devices, (list, tuple)):
            device_items = []
            for entry in raw_devices:
                if not isinstance(entry, dict):
                    raise AudioError("INVALID_STATE", {"reason": "bad_device"})
                item_id = entry.get("device_id", entry.get("id"))
                if item_id is None:
                    raise AudioError("INVALID_STATE", {"reason": "bad_device"})
                device_items.append((item_id, entry))
        else:
            raise AudioError("INVALID_STATE", {"reason": "bad_devices"})

        devices = {}
        order = []
        for device_id, raw in device_items:
            if not isinstance(raw, dict):
                raise AudioError("INVALID_STATE", {"reason": "bad_device"})
            rate = raw.get("sample_rate", engine_rate)
            channels = raw.get("channels", engine_channels)
            try:
                rate = int(rate)
                channels = int(channels)
                if rate <= 0 or channels <= 0:
                    raise ValueError("invalid device format")
                device = _Device(
                    device_id,
                    rate,
                    channels,
                    latency=float(raw.get("latency", 0.0)),
                    healthy=bool(raw.get("healthy", True)),
                    exclusive=bool(raw.get("exclusive", False)),
                    voices=raw.get("voices", raw.get("voice_ids", ())),
                )
            except (TypeError, ValueError, AudioError) as exc:
                raise AudioError("INVALID_STATE", {"device": device_id}) from exc
            device.present = bool(raw.get("present", True))
            device.released = bool(raw.get("released", not device.present))
            device.draining = bool(raw.get("draining", False))
            device.initialized = bool(raw.get("initialized", device.healthy))
            device.generation = int(raw.get("generation", 0))
            device.buffered_frames = int(raw.get("buffered_frames", 0))
            device.refcount = int(raw.get("refcount", 1 if device.present else 0))
            device.in_flight = int(raw.get("in_flight", 0))
            devices[device_id] = device
            order.append(device_id)

        saved_order = state.get("device_order")
        if isinstance(saved_order, (list, tuple)):
            order = [
                device_id
                for device_id in saved_order
                if device_id in devices
            ] + [
                device_id
                for device_id in devices
                if device_id not in saved_order
            ]

        playhead = state.get("playhead_fraction")
        if isinstance(playhead, (list, tuple)) and len(playhead) == 2:
            try:
                self._playhead = Fraction(int(playhead[0]), int(playhead[1]))
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise AudioError(
                    "INVALID_STATE",
                    {"reason": "bad_playhead"},
                ) from exc
        else:
            seconds = state.get("playhead_seconds", state.get("playhead", 0.0))
            self._playhead = Fraction(str(float(seconds)))

        played = state.get("played_samples_fraction")
        if isinstance(played, (list, tuple)) and len(played) == 2:
            try:
                self._played_samples = Fraction(int(played[0]), int(played[1]))
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise AudioError(
                    "INVALID_STATE",
                    {"reason": "bad_playhead"},
                ) from exc
        else:
            self._played_samples = Fraction(
                int(state.get("played_samples", 0)),
                1,
            )

        current = state.get("current")
        if current == "null":
            current = None
        if current is not None:
            current_device = devices.get(current)
            if (
                current_device is None
                or not current_device.present
                or not current_device.healthy
            ):
                current = None

        self.engine_sample_rate = engine_rate
        self.engine_channels = engine_channels
        self._devices = devices
        self._device_order = order
        self._current = current
        self.sample_rate = int(state.get("sample_rate", engine_rate))
        self.channels = int(state.get("channels", engine_channels))
        if current is not None:
            self.sample_rate = self._devices[current].sample_rate
            self.channels = self._devices[current].channels
        self._format_generation = int(state.get("format_generation", 0))
        self._bus_generation = int(state.get("bus_generation", 0))
        self._resampler_epoch = int(state.get("resampler_epoch", 0))
        self._resampler_state = None
        self._bus_channel_map = _channel_map(self.channels)
        self._latency = float(state.get("latency", 0.0))
        self._compensation_samples = int(
            state.get(
                "compensation_samples",
                round(self._latency * self.sample_rate),
            )
        )
        recovery = state.get("recovery")
        self._recovery = dict(recovery) if isinstance(recovery, dict) else None
        self._switch_serial = int(state.get("switch_serial", 0))
        self._null_view = self._make_null_view()
        self._refresh_all_views()

        return {
            "loaded": True,
            "device": self._current_label(),
            "playhead": float(self._playhead),
        }

    def _is_active(self, device):
        return (
            device.present
            and not device.released
            and device.healthy
            and device.initialized
        )

    def _current_label(self):
        return "null" if self._current is None else self._current

    def _apply_bus_format(self, sample_rate, channels):
        old_rate = self.sample_rate
        old_channels = self.channels
        changed = old_rate != sample_rate or old_channels != channels

        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self._bus_channel_map = _channel_map(self.channels)
        self._format_generation += 1
        if changed:
            self._bus_generation += 1
            self._resampler_epoch += 1
            self._resampler_state = None

        self._compensation_samples = int(
            round(self._latency * self.sample_rate)
        )
        self._notify_mixer_latency()
        return {
            "from_sample_rate": old_rate,
            "from_channels": old_channels,
            "bus_rebuilt": changed,
            "resampler_reset": changed,
            "resampler_epoch": self._resampler_epoch,
            "bus_generation": self._bus_generation,
            "compensation_samples": self._compensation_samples,
        }

    def _fallback_to_null(self, reason, requested=None):
        previous = self._current
        previous_device = (
            self._devices.get(previous) if previous is not None else None
        )
        if previous_device is not None and previous_device.present:
            previous_device.draining = True

        self._current = None
        bus_info = self._apply_bus_format(
            self.engine_sample_rate,
            self.engine_channels,
        )
        if requested is not None:
            self._recovery = {
                "device": requested,
                "reason": reason,
            }
        else:
            self._recovery = None
        self._refresh_all_views()
        return {
            "switched": True,
            "device": "null",
            "previous": previous if previous is not None else "null",
            "reason": reason,
            "requested": requested,
            "recovery": dict(self._recovery) if self._recovery else None,
            "playhead": float(self._playhead),
            "played_samples": int(self._played_samples),
            **bus_info,
        }

    def _clear_recovery(self, device_id):
        if self._recovery and self._recovery.get("device") == device_id:
            self._recovery = None

    def _notify_mixer_latency(self):
        if self.mixer is None:
            return False
        for method_name in (
            "recompute_latency_compensation",
            "set_latency_compensation",
            "update_latency",
            "set_compensation",
            "recompute_compensation",
            "set_latency",
            "latency_changed",
        ):
            method = getattr(self.mixer, method_name, None)
            if method is not None:
                try:
                    method(self._compensation_samples, self.sample_rate)
                except TypeError:
                    method(self._compensation_samples)
                return True
        return False

    def _refresh_all_views(self):
        self._null_view = self._make_null_view()
        for device in self._devices.values():
            self._refresh_device_view(device)

    def _refresh_device_view(self, device):
        device.refcount = (
            1 if device.present and not device.released else 0
        ) + device.in_flight
        device.callback_view = {
            "device": device.device_id,
            "device_id": device.device_id,
            "sample_rate": device.sample_rate,
            "channels": device.channels,
            "channel_map": device.channel_map,
            "healthy": device.healthy,
            "exclusive": device.exclusive,
            "generation": device.generation,
            "current": self._current == device.device_id,
            "draining": device.draining,
            "render_voices": (
                self._current == device.device_id
                and device.present
                and not device.released
                and device.healthy
            ),
            "engine_sample_rate": self.engine_sample_rate,
            "resampling_ratio": (
                device.sample_rate / self.engine_sample_rate
                if self.engine_sample_rate
                else 1.0
            ),
            "compensation_samples": int(
                round(self._latency * device.sample_rate)
            ),
            "buffered_frames": device.buffered_frames,
            "playhead": float(self._playhead),
            "played_samples": int(self._played_samples),
        }

    def _update_playback_views(self):
        playhead = float(self._playhead)
        played_samples = int(self._played_samples)
        self._null_view["playhead"] = playhead
        self._null_view["played_samples"] = played_samples
        for device in self._devices.values():
            view = device.callback_view
            if view is not None:
                view["playhead"] = playhead
                view["played_samples"] = played_samples

    def _make_null_view(self):
        return {
            "device": "null",
            "device_id": "null",
            "null": True,
            "sample_rate": self.engine_sample_rate,
            "channels": self.engine_channels,
            "channel_map": _channel_map(self.engine_channels),
            "healthy": True,
            "exclusive": False,
            "generation": self._format_generation,
            "current": self._current is None,
            "draining": False,
            "render_voices": False,
            "engine_sample_rate": self.engine_sample_rate,
            "resampling_ratio": 1.0,
            "compensation_samples": self._compensation_samples,
            "buffered_frames": 0,
            "playhead": float(self._playhead),
            "played_samples": int(self._played_samples),
        }

    def _null_format(self):
        return {
            "device": "null",
            "device_id": "null",
            "null": True,
            "sample_rate": self.engine_sample_rate,
            "channels": self.engine_channels,
            "channel_map": _channel_map(self.engine_channels),
            "latency": self._latency,
            "compensation_samples": int(
                round(self._latency * self.engine_sample_rate)
            ),
            "generation": self._format_generation,
        }

    def _device_format(self, device):
        return {
            "device": device.device_id,
            "device_id": device.device_id,
            "sample_rate": device.sample_rate,
            "channels": device.channels,
            "channel_map": device.channel_map,
            "latency": device.latency,
            "compensation_samples": int(
                round(self._latency * device.sample_rate)
            ),
            "generation": device.generation,
            "engine_sample_rate": self.engine_sample_rate,
            "resampling_ratio": (
                device.sample_rate / self.engine_sample_rate
                if self.engine_sample_rate
                else 1.0
            ),
        }
