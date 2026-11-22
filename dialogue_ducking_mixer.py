class DuckingError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class DialogueDuckingMixer:
    """Sample-accurate dialogue ducking envelope for music buses."""

    def __init__(self, sample_rate=48000, attack_seconds=0.01,
                 release_seconds=0.2, min_speech_seconds=0.05,
                 duck_gain=0.25):
        self.sample_rate = sample_rate
        self.attack_seconds = attack_seconds
        self.release_seconds = release_seconds
        self.min_speech_seconds = min_speech_seconds
        self.duck_gain = duck_gain

    def merge_voices(self, voices):
        raise NotImplementedError

    def process(self, speech_flags, base_gain=1.0):
        raise NotImplementedError

    def process_block(self, speech_flags, base_gain=1.0):
        raise NotImplementedError

    def set_snapshot(self, active):
        raise NotImplementedError

    def force_release(self):
        raise NotImplementedError

    def attack_samples(self):
        raise NotImplementedError

    def release_samples(self):
        raise NotImplementedError

    def envelope(self):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
