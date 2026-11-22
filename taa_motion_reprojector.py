class TaaError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TaaMotionReprojector:
    """Temporal anti-aliasing history and screen-space motion vectors."""

    def __init__(self, blend=0.1, max_history=0.9):
        self.blend = blend
        self.max_history = max_history

    def begin_frame(self, camera_id, projection, jitter=(0.0, 0.0),
                    resolution=(64, 64)):
        raise NotImplementedError

    def object_motion(self, object_id, prev_ndc, curr_ndc, teleport=False):
        raise NotImplementedError

    def reproject(self, history_index, motion, resolution=None):
        raise NotImplementedError

    def should_accumulate(self, layer):
        raise NotImplementedError

    def invalidate_history(self, reason, view_id="main"):
        raise NotImplementedError

    def commit(self, history_version):
        raise NotImplementedError

    def frame_state(self):
        raise NotImplementedError

    def set_resolution(self, resolution):
        raise NotImplementedError

    def create_view(self, view_id):
        raise NotImplementedError

    def history_valid(self, view_id="main"):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
