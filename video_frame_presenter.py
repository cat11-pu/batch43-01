class PresenterError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class VideoFramePresenter:
    """Audio-master-clock video presentation queue."""

    def __init__(self, refresh_hz=60.0, capacity=3, audio_latency=0.0):
        self.refresh_hz = refresh_hz
        self.capacity = capacity
        self.audio_latency = audio_latency

    def submit(self, frame_id, pts, now=None):
        raise NotImplementedError

    def present(self, now=None):
        raise NotImplementedError

    def set_audio_clock(self, pts):
        raise NotImplementedError

    def seek(self, target_pts):
        raise NotImplementedError

    def pause(self):
        raise NotImplementedError

    def resume(self):
        raise NotImplementedError

    def set_refresh(self, hz):
        raise NotImplementedError

    def set_hdr(self, enabled):
        raise NotImplementedError

    def set_audio_latency(self, seconds):
        raise NotImplementedError

    def create_view(self, view_id):
        raise NotImplementedError

    def queue(self):
        raise NotImplementedError

    def presented(self):
        raise NotImplementedError

    def state(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
