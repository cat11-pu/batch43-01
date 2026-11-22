class AbrError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AbrVariantLadder:
    """ABR variant ladder with boundary-only atomic switches."""

    def __init__(self, mode="live"):
        self.mode = mode

    def load_manifest(self, variants):
        raise NotImplementedError

    def add_segment(self, seq, info):
        raise NotImplementedError

    def decide(self, video_buffer=10.0, audio_buffer=10.0,
               bandwidth_bps=None):
        raise NotImplementedError

    def request_switch(self, target, seq):
        raise NotImplementedError

    def on_segment_end(self, seq, bandwidth_bps=None, stall=False):
        raise NotImplementedError

    def current(self):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError

    def seek(self, seq):
        raise NotImplementedError

    def init_segment(self, variant_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
