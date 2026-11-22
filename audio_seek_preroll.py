class AudioError(ValueError):
    """Raised when an audio seek or indexing request is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AudioSeekPreroll:
    """Frame indexed seeking with preroll discard and loop boundaries."""

    def __init__(self, sample_rate=48000, encoder_delay=1024,
                 tail_padding=512, preroll_ms=50):
        self.sample_rate = sample_rate
        self.encoder_delay = encoder_delay
        self.tail_padding = tail_padding
        self.preroll_ms = preroll_ms

    def load(self, track_id, frames, sample_rate=None, vbr=False,
             size_bytes=None, bitrate=None):
        raise NotImplementedError

    def duration_ms(self, track_id):
        raise NotImplementedError

    def total_frames(self, track_id):
        raise NotImplementedError

    def seek(self, track_id, target_ms, request_id=None):
        raise NotImplementedError

    def read(self, request_id, count):
        raise NotImplementedError

    def cancel(self, request_id):
        raise NotImplementedError

    def active_request(self, track_id):
        raise NotImplementedError

    def loop_points(self, track_id, start_ms, end_ms):
        raise NotImplementedError

    def playlist_gap(self, first_track, second_track):
        raise NotImplementedError

    def set_output(self, channels):
        raise NotImplementedError

    def output_state(self):
        raise NotImplementedError

    def pause(self, now_ms):
        raise NotImplementedError

    def resume(self, now_ms):
        raise NotImplementedError
