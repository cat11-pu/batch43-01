from dataclasses import dataclass, field


class LaneError(ValueError):
    """Raised when a simulcast lane request is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _Subscription:
    subscriber: object
    stream: str
    active: str
    desired: str
    display: str
    pending: bool = False
    keyframe_requests: int = 0
    requested_frames: set = field(default_factory=set)
    last_keyframe_at: dict = field(default_factory=dict)
    last_applied_frame: object = None

    def as_dict(self):
        return {
            "subscriber": self.subscriber,
            "stream": self.stream,
            "active": self.active,
            "desired": self.desired,
            "display": self.display,
            "pending": self.pending,
            "keyframe_requests": self.keyframe_requests,
            "requested_frames": sorted(self.requested_frames, key=repr),
            "last_keyframe_at": dict(self.last_keyframe_at),
            "last_applied_frame": self.last_applied_frame,
        }


class SimulcastLaneSelect:
    """Per-subscriber layer selection with keyframe gated switching."""

    def __init__(self, layers=("low", "mid", "high")):
        self.layers = tuple(layers)
        if not self.layers or len(set(self.layers)) != len(self.layers):
            raise LaneError("invalid_layers")
        self._subscriptions = {}
        self._publisher_ready = True

    def _key(self, subscriber, stream):
        return subscriber, stream

    def _require(self, subscriber, stream):
        if not self._publisher_ready:
            raise LaneError("not_subscribed")
        subscription = self._subscriptions.get(self._key(subscriber, stream))
        if subscription is None:
            raise LaneError("not_subscribed")
        return subscription

    def _validate_layer(self, layer):
        if layer not in self.layers:
            raise LaneError("unknown_layer", {"layer": layer})

    def _status(self, status, subscription, **extra):
        result = {
            "status": status,
            "subscriber": subscription.subscriber,
            "stream": subscription.stream,
            "active": subscription.active,
            "desired": subscription.desired,
            "display": subscription.display,
            "pending": subscription.pending,
            "keyframe_requests": subscription.keyframe_requests,
        }
        result.update(extra)
        return result

    def _request_keyframe(self, subscription, now, frame=None):
        if frame is not None and frame in subscription.requested_frames:
            return "duplicate"

        target = subscription.desired
        last = subscription.last_keyframe_at.get(target)
        if last is not None and now - last < 1:
            return "throttled"

        if frame is not None:
            subscription.requested_frames.add(frame)
        subscription.last_keyframe_at[target] = now
        subscription.keyframe_requests += 1
        return "requested"

    def subscribe(self, subscriber, stream="camera", layer="low", now=0):
        self._validate_layer(layer)
        key = self._key(subscriber, stream)
        existing = self._subscriptions.get(key)
        if existing is not None:
            return self._status("subscribed", existing)

        subscription = _Subscription(
            subscriber=subscriber,
            stream=stream,
            active=layer,
            desired=layer,
            display=layer,
        )
        self._subscriptions[key] = subscription
        return self._status("subscribed", subscription)

    def unsubscribe(self, subscriber, stream="camera"):
        key = self._key(subscriber, stream)
        subscription = self._subscriptions.pop(key, None)
        if subscription is None:
            raise LaneError("not_subscribed")
        return {
            "status": "unsubscribed",
            "subscriber": subscriber,
            "stream": stream,
        }

    def select(self, subscriber, stream, layer, now=0):
        subscription = self._require(subscriber, stream)
        self._validate_layer(layer)

        if subscription.desired == layer:
            return self._status("same_layer", subscription)

        if subscription.active == layer:
            subscription.pending = False
            subscription.desired = subscription.active
            subscription.display = subscription.active
            return self._status("same_layer", subscription)

        subscription.desired = layer
        subscription.pending = True
        self._request_keyframe(subscription, now)
        return self._status("pending", subscription)

    def request_keyframe(self, subscriber, stream, frame, now=0):
        subscription = self._require(subscriber, stream)
        status = self._request_keyframe(subscription, now, frame=frame)
        return self._status(status, subscription, frame=frame)

    def apply(self, subscriber, stream, layer, frame, keyframe=True, now=0):
        subscription = self._require(subscriber, stream)
        self._validate_layer(layer)

        if not subscription.pending:
            if layer == subscription.active:
                return self._status("same_layer", subscription, frame=frame)
            raise LaneError(
                "unexpected_layer",
                {"expected": subscription.active, "received": layer},
            )

        if layer != subscription.desired:
            raise LaneError(
                "unexpected_layer",
                {"expected": subscription.desired, "received": layer},
            )

        if not keyframe:
            return self._status(
                "waiting_keyframe", subscription, frame=frame
            )

        subscription.active = layer
        subscription.display = layer
        subscription.desired = layer
        subscription.pending = False
        subscription.last_applied_frame = frame
        return self._status("switched", subscription, frame=frame)

    def reconnect(self, subscriber, stream, now=0):
        subscription = self._require(subscriber, stream)
        subscription.pending = False
        subscription.desired = subscription.active
        subscription.display = subscription.active
        subscription.last_keyframe_at.clear()
        subscription.requested_frames.clear()
        return self._status("reconnected", subscription)

    def current(self, subscriber, stream):
        subscription = self._require(subscriber, stream)
        return {
            "subscriber": subscription.subscriber,
            "stream": subscription.stream,
            "active": subscription.active,
            "desired": subscription.desired,
            "display": subscription.display,
            "pending": subscription.pending,
            "keyframe_requests": subscription.keyframe_requests,
        }

    def snapshot(self):
        return {
            "layers": list(self.layers),
            "subscriptions": [
                self._subscriptions[key].as_dict()
                for key in sorted(self._subscriptions, key=repr)
            ],
        }

    def publisher_restart(self, now=0):
        self._subscriptions.clear()
        self._publisher_ready = False
        return {
            "status": "restarted",
            "subscriptions": 0,
            "restored": False,
        }

    def restore(self, state):
        if isinstance(state, dict):
            subscriptions = state.get("subscriptions")
        else:
            subscriptions = state
        if not isinstance(subscriptions, (list, tuple)):
            raise LaneError("invalid_state")

        restored = {}
        for item in subscriptions:
            if not isinstance(item, dict):
                raise LaneError("invalid_state")
            try:
                subscriber = item["subscriber"]
                stream = item["stream"]
                active = item["active"]
            except KeyError as exc:
                raise LaneError("invalid_state", {"missing": str(exc)}) from exc

            if active not in self.layers:
                raise LaneError("invalid_state", {"layer": active})

            key = self._key(subscriber, stream)
            if key in restored:
                raise LaneError("invalid_state", {"duplicate": repr(key)})

            desired = item.get("desired", active)
            display = item.get("display", active)
            if desired not in self.layers or display not in self.layers:
                raise LaneError("invalid_state")

            pending = bool(item.get("pending", False))
            if pending or desired != active:
                desired = active
                display = active
                pending = False

            requested_frames = item.get("requested_frames", [])
            if not isinstance(requested_frames, (list, tuple, set)):
                raise LaneError("invalid_state")
            timestamps = item.get("last_keyframe_at", {})
            if not isinstance(timestamps, dict):
                raise LaneError("invalid_state")

            restored[key] = _Subscription(
                subscriber=subscriber,
                stream=stream,
                active=active,
                desired=desired,
                display=display,
                pending=pending,
                keyframe_requests=int(item.get("keyframe_requests", 0)),
                requested_frames=set(requested_frames),
                last_keyframe_at={
                    layer: timestamp
                    for layer, timestamp in timestamps.items()
                    if layer in self.layers
                },
                last_applied_frame=item.get("last_applied_frame"),
            )

        self._subscriptions = restored
        self._publisher_ready = True
        return {
            "status": "restored",
            "subscriptions": len(self._subscriptions),
        }
