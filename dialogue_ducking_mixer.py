class DuckingError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class DialogueDuckingMixer:
    """Sample-accurate dialogue ducking envelope for music buses.

    The ducking gain is generated one sample at a time so the envelope is
    continuous across block and speech boundaries.  All channels share one
    speech decision and one envelope, and the same gain is applied to the dry
    music bus and to its reverb send before the master limiter.
    """

    def __init__(self, sample_rate=48000, attack_seconds=0.01,
                 release_seconds=0.2, min_speech_seconds=0.05,
                 duck_gain=0.25):
        if sample_rate <= 0:
            raise DuckingError("invalid_sample_rate", sample_rate)
        if attack_seconds <= 0:
            raise DuckingError("invalid_attack_seconds", attack_seconds)
        if release_seconds <= 0:
            raise DuckingError("invalid_release_seconds", release_seconds)
        if min_speech_seconds <= 0:
            raise DuckingError("invalid_min_speech_seconds", min_speech_seconds)
        if not 0.0 < duck_gain <= 1.0:
            raise DuckingError("invalid_duck_gain", duck_gain)

        self.sample_rate = sample_rate
        self.attack_seconds = attack_seconds
        self.release_seconds = release_seconds
        self.min_speech_seconds = min_speech_seconds
        self.duck_gain = duck_gain
        self._attack_samples = max(1, round(attack_seconds * sample_rate))
        self._release_samples = max(1, round(release_seconds * sample_rate))
        self._min_speech_samples = max(
            1, round(min_speech_seconds * sample_rate))
        self.reset()

    def merge_voices(self, voices):
        """Merge several voice streams with a logical OR (max, never sum)."""
        streams = [list(voice) for voice in voices]
        if not streams:
            return []
        length = max(len(voice) for voice in streams)
        merged = []
        for index in range(length):
            active = any(index < len(voice) and voice[index]
                         for voice in streams)
            merged.append(1 if active else 0)
        return merged

    def process(self, speech_flags, base_gain=1.0):
        return self._run(speech_flags, base_gain)

    def process_block(self, speech_flags, base_gain=1.0):
        return self._run(speech_flags, base_gain)

    def set_snapshot(self, active):
        """Reset the envelope on snapshot load / timeline jump.

        Any in-flight duck is dropped and the gain returns to the pre-duck
        baseline so later blocks continue from a clean, smooth state.
        """
        self._snapshot_active = bool(active)
        self._speech_active = False
        self._speech_run = 0
        self._gain = self._baseline

    def force_release(self):
        """Handle an abnormal stream end: clear speech without a jump.

        The envelope keeps its current level and ramps back to the baseline on
        subsequent blocks, so there is no audible step.
        """
        self._speech_active = False
        self._speech_run = 0

    def attack_samples(self):
        return self._attack_samples

    def release_samples(self):
        return self._release_samples

    def envelope(self):
        return self._gain

    def state(self):
        return {
            "music_gain": self._gain,
            "channel_gains": (self._gain, self._gain),
            "baseline_gain": self._baseline,
            "speech_active": self._speech_active,
            "snapshot_active": self._snapshot_active,
            "sample_rate": self.sample_rate,
        }

    def reset(self):
        self._speech_active = False
        self._speech_run = 0
        self._baseline = 1.0
        self._gain = 1.0
        self._snapshot_active = False

    def dump(self):
        return {
            "sample_rate": self.sample_rate,
            "attack_seconds": self.attack_seconds,
            "release_seconds": self.release_seconds,
            "min_speech_seconds": self.min_speech_seconds,
            "duck_gain": self.duck_gain,
            "gain": self._gain,
            "baseline": self._baseline,
            "speech_active": self._speech_active,
            "speech_run": self._speech_run,
            "snapshot_active": self._snapshot_active,
        }

    def load(self, state):
        if not isinstance(state, dict) or "sample_rate" not in state:
            raise DuckingError("invalid_state", state)
        self.__init__(
            sample_rate=state["sample_rate"],
            attack_seconds=state["attack_seconds"],
            release_seconds=state["release_seconds"],
            min_speech_seconds=state["min_speech_seconds"],
            duck_gain=state["duck_gain"],
        )
        self._gain = state["gain"]
        self._baseline = state["baseline"]
        self._speech_active = state["speech_active"]
        self._speech_run = state["speech_run"]
        self._snapshot_active = state["snapshot_active"]

    def _run(self, speech_flags, base_gain):
        base_gain = float(base_gain)
        if (not self._speech_active and self._speech_run == 0
                and self._gain == self._baseline
                and (not self._snapshot_active)):
            self._baseline = base_gain
            self._gain = base_gain

        floor = base_gain * self.duck_gain
        attack_alpha = 0.5 / self._attack_samples
        release_step = (base_gain - floor) / self._release_samples

        flags = [1 if flag else 0 for flag in speech_flags]
        was_active = self._speech_active
        confirmed = self._confirm_speech(flags)

        gains = []
        for index, flag in enumerate(flags):
            active = confirmed[index]
            if active and not was_active:
                self._baseline = base_gain
            was_active = active
            self._step_envelope(active, floor, attack_alpha, release_step)
            gains.append(self._gain)

        return {"music": gains, "send": list(gains)}

    def _confirm_speech(self, flags):
        """Hysteretic detector with a minimum speech duration.

        Speech only becomes active once ``min_speech_samples`` consecutive
        flags are seen.  A run that is still unconfirmed at the end of the
        block is carried over so block-wise processing stays sample-identical
        to whole-buffer processing.  Silence frames never trigger speech.
        """
        confirmed = [0] * len(flags)
        run = self._speech_run
        active = self._speech_active
        for index, flag in enumerate(flags):
            if flag:
                run += 1
                if run >= self._min_speech_samples:
                    active = True
                    confirmed[index] = 1
            else:
                run = 0
                active = False

        if flags and flags[-1]:
            self._speech_run = run
        else:
            self._speech_run = 0
        self._speech_active = active
        return confirmed

    def _step_envelope(self, active, floor, attack_alpha, release_step):
        if active:
            if self._gain > floor:
                self._gain -= (self._gain - floor) * attack_alpha
                if self._gain < floor:
                    self._gain = floor
            return

        baseline = self._baseline
        if self._gain < baseline:
            remaining = baseline - self._gain
            if remaining <= release_step:
                self._gain = baseline
            else:
                self._gain += release_step
