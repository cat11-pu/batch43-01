class VoiceError(ValueError):
    """Raised when a voice activity request is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VoiceActivityGate:
    """Voice detection, hangover gating, ducking and device recovery."""

    def __init__(self, hangover_ms=300, threshold_db=-40, duck_db=12):
        self.hangover_ms = hangover_ms
        self.threshold_db = threshold_db
        self.duck_db = duck_db

    def detect(self, user, level_db, now_ms, push_to_talk=False,
               sample_rate=48000):
        raise NotImplementedError

    def gate(self, user, now_ms):
        raise NotImplementedError

    def duck(self, buses, active):
        raise NotImplementedError

    def set_mute(self, user, muted):
        raise NotImplementedError

    def set_device(self, user, device):
        raise NotImplementedError

    def hotplug(self, user, device):
        raise NotImplementedError

    def recover(self, user=None):
        raise NotImplementedError

    def state(self, user):
        raise NotImplementedError

    def active_users(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
