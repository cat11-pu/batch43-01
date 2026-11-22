class AudioError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AudioDeviceSwitchboard:
    """Audio output device list, format migration and voices."""

    def __init__(self, sample_rate=48000, channels=2):
        self.sample_rate = sample_rate
        self.channels = channels

    def add_device(self, device_id, sample_rate=None, channels=None,
                   latency=0.0, healthy=True, exclusive=False):
        raise NotImplementedError

    def remove_device(self, device_id):
        raise NotImplementedError

    def drain(self, device_id):
        raise NotImplementedError

    def set_default(self, device_id):
        raise NotImplementedError

    def current(self):
        raise NotImplementedError

    def active_devices(self):
        raise NotImplementedError

    def format(self, device_id=None):
        raise NotImplementedError

    def playhead(self):
        raise NotImplementedError

    def advance(self, samples):
        raise NotImplementedError

    def callback(self, device_id):
        raise NotImplementedError

    def add_voice(self, device_id, voice_id):
        raise NotImplementedError

    def voice_count(self, device_id):
        raise NotImplementedError

    def set_audio_latency(self, seconds):
        raise NotImplementedError

    def lose_exclusive(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
