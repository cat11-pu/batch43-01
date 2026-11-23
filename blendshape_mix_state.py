import math


class BlendshapeError(ValueError):
    """Raised when a blendshape weight operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_BASE_POSITION = (0.0, 0.0, 0.0)
_BASE_NORMAL = (0.0, 0.0, 1.0)
_SUM_TOLERANCE = 1e-6


class _CharacterState:
    """Per-character blendshape state; characters never share buffers."""

    __slots__ = ("weights", "pending", "lod", "last_frame", "events")

    def __init__(self):
        self.weights = {}
        self.pending = None
        self.lod = "high"
        self.last_frame = None
        self.events = []


class BlendshapeMixState:
    """Normalized blendshape weights, frame ordering and LOD channel mapping."""

    def __init__(self, channels=None, lod_map=None):
        self.channels = dict(channels or {})
        self.lod_map = dict(lod_map or {})
        self._characters = {}
        self._device_lost = False
        self._generation = 0

    def _state(self, character):
        state = self._characters.get(character)
        if state is None:
            state = _CharacterState()
            self._characters[character] = state
        return state

    @staticmethod
    def _is_stale(state, frame):
        return state.last_frame is not None and frame < state.last_frame

    def _validate_weights(self, weights):
        total = 0.0
        for name, weight in weights.items():
            if name not in self.channels:
                raise BlendshapeError("unknown_channel", name)
            if weight < 0:
                raise BlendshapeError("negative_weight", weight)
            total += weight
        if total > 1.0 + _SUM_TOLERANCE:
            raise BlendshapeError("weights_exceed_one", total)

    def _blend(self, weights):
        total = sum(weights.values())
        base = max(0.0, 1.0 - total)
        px, py, pz = (_BASE_POSITION[i] * base for i in range(3))
        nx, ny, nz = (_BASE_NORMAL[i] * base for i in range(3))
        for name, weight in weights.items():
            channel = self.channels.get(name)
            if channel is None:
                continue
            position = channel["position"]
            normal = channel["normal"]
            px += float(position[0]) * weight
            py += float(position[1]) * weight
            pz += float(position[2]) * weight
            nx += float(normal[0]) * weight
            ny += float(normal[1]) * weight
            nz += float(normal[2]) * weight
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length > 0.0:
            nx, ny, nz = nx / length, ny / length, nz / length
        else:
            nx, ny, nz = _BASE_NORMAL
        return (px, py, pz), (nx, ny, nz)

    def set(self, character, channel, weight, frame=0):
        state = self._state(character)
        if self._is_stale(state, frame):
            return {"status": "stale_frame", "frame": frame}
        if channel not in self.channels:
            raise BlendshapeError("unknown_channel", channel)
        if weight < 0:
            raise BlendshapeError("negative_weight", weight)
        new_weights = dict(state.weights)
        if weight == 0:
            new_weights.pop(channel, None)
        else:
            new_weights[channel] = float(weight)
        self._validate_weights(new_weights)
        state.weights = new_weights
        state.pending = None
        state.last_frame = frame
        return {"status": "ok", "frame": frame, "weights": dict(state.weights)}

    def mix(self, character, weights=None, frame=0):
        state = self._state(character)
        if self._is_stale(state, frame):
            return {"status": "stale_frame", "frame": frame}
        weights = dict(weights or {})
        self._validate_weights(weights)
        state.pending = {
            name: float(weight) for name, weight in weights.items() if weight != 0
        }
        state.last_frame = frame
        return {"status": "ok", "frame": frame, "weights": dict(state.pending)}

    def apply(self, character, frame=0):
        state = self._state(character)
        if self._is_stale(state, frame):
            return {"status": "stale_frame", "frame": frame}
        if state.pending is not None:
            state.weights = state.pending
            state.pending = None
        state.last_frame = frame
        position, normal = self._blend(state.weights)
        return {
            "status": "ok",
            "frame": frame,
            "position": position,
            "normal": normal,
            "weights": dict(state.weights),
        }

    def reset(self, character):
        state = self._state(character)
        state.weights = {}
        state.pending = None
        return {"status": "ok"}

    def set_lod(self, character, lod):
        state = self._state(character)
        state.lod = lod
        return {"status": "ok", "lod": lod}

    def weights(self, character):
        state = self._characters.get(character)
        if state is None:
            return {}
        if state.lod != "high":
            return {
                self.lod_map.get(name, name): weight
                for name, weight in state.weights.items()
            }
        return dict(state.weights)

    def events(self, character):
        state = self._characters.get(character)
        if state is None:
            return []
        return list(state.events)

    def emit_event(self, character, name, frame=0):
        state = self._state(character)
        event = {"name": name, "frame": frame, "weights": dict(state.weights)}
        state.events.append(event)
        return event

    def reload_channels(self, channels, lod_map=None):
        self.channels = dict(channels or {})
        if lod_map is not None:
            self.lod_map = dict(lod_map)
        self._generation += 1
        for state in self._characters.values():
            state.weights = {
                name: weight
                for name, weight in state.weights.items()
                if name in self.channels
            }
            if state.pending is not None:
                state.pending = {
                    name: weight
                    for name, weight in state.pending.items()
                    if name in self.channels
                }
        return {"status": "ok", "generation": self._generation}

    def device_lost(self):
        self._device_lost = True
        return {"status": "device_lost"}

    def device_restored(self):
        self._device_lost = False
        return {"status": "ok"}

    def dump(self):
        return {
            "version": 1,
            "characters": {
                name: {
                    "weights": dict(state.weights),
                    "lod": state.lod,
                    "frame": state.last_frame,
                }
                for name, state in self._characters.items()
            },
        }

    def load(self, state):
        self._characters = {}
        for name, saved in (state.get("characters") or {}).items():
            character = _CharacterState()
            character.weights = {
                channel: float(weight)
                for channel, weight in (saved.get("weights") or {}).items()
            }
            character.lod = saved.get("lod", "high")
            character.last_frame = saved.get("frame")
            self._characters[name] = character
        return {"status": "ok"}
