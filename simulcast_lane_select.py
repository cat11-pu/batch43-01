class LaneError(ValueError):
    """Raised when a simulcast lane request is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SimulcastLaneSelect:
    """Per-subscriber layer selection with keyframe gated switching."""

    def __init__(self, layers=("low", "mid", "high")):
        self.layers = tuple(layers)

    def subscribe(self, subscriber, stream="camera", layer="low", now=0):
        raise NotImplementedError

    def unsubscribe(self, subscriber, stream="camera"):
        raise NotImplementedError

    def select(self, subscriber, stream, layer, now=0):
        raise NotImplementedError

    def request_keyframe(self, subscriber, stream, frame, now=0):
        raise NotImplementedError

    def apply(self, subscriber, stream, layer, frame, keyframe=True, now=0):
        raise NotImplementedError

    def reconnect(self, subscriber, stream, now=0):
        raise NotImplementedError

    def current(self, subscriber, stream):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def publisher_restart(self, now=0):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
