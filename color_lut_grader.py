import json
import math
import threading


class LutError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class _LutSnapshot:
    __slots__ = (
        "entries",
        "size",
        "domain",
        "version",
        "flip",
        "vector",
        "padding",
    )

    def __init__(self, entries, size, domain, version, flip, vector, padding):
        self.entries = entries
        self.size = size
        self.domain = domain
        self.version = version
        self.flip = flip
        self.vector = vector
        self.padding = padding


class ColorLutGrader:
    """Color LUT sampling, strength blending and atomic hot reload."""

    WORKING_SPACE = "linear"
    OUTPUT_SPACE = "linear"
    ATLAS_PADDING = 1
    QUANTIZATION_ERROR = 1.0 / 255.0
    SUPPORTED_SPACES = frozenset(("linear", "srgb"))

    def __init__(self, strength=1.0, exposure=1.0,
                 input_space=WORKING_SPACE, output_space=OUTPUT_SPACE,
                 tone_map=None):
        self._lock = threading.RLock()
        self._lut = None
        self._strength = self._coerce_strength(strength)
        self._exposure = self._coerce_exposure(exposure)
        self._input_space = self._coerce_space(input_space, "input_space")
        self._output_space = self._coerce_space(output_space, "output_space")
        self._tone_map = self._coerce_tone_map(tone_map)

    @staticmethod
    def _coerce_strength(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise LutError("INVALID_STRENGTH", value) from None
        if not math.isfinite(value):
            raise LutError("INVALID_STRENGTH", value)
        return min(1.0, max(0.0, value))

    @staticmethod
    def _coerce_exposure(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise LutError("INVALID_EXPOSURE", value) from None
        if not math.isfinite(value) or value < 0.0:
            raise LutError("INVALID_EXPOSURE", value)
        return value

    @classmethod
    def _coerce_space(cls, value, field):
        if not isinstance(value, str) or value.lower() not in cls.SUPPORTED_SPACES:
            raise LutError("UNSUPPORTED_COLOR_SPACE", (field, value))
        return value.lower()

    @staticmethod
    def _coerce_tone_map(value):
        if value is None or callable(value):
            return value
        raise LutError("INVALID_TONE_MAP", value)

    @staticmethod
    def _sanitize_input(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        if math.isnan(value) or value <= 0.0:
            return 0.0
        if math.isinf(value):
            return 1.0
        return value

    @classmethod
    def _sanitize_color(cls, rgb):
        if not isinstance(rgb, (tuple, list)) or len(rgb) != 3:
            raise LutError("INVALID_COLOR", rgb)
        return tuple(cls._sanitize_input(channel) for channel in rgb)

    @classmethod
    def _srgb_to_linear(cls, value):
        if value <= 0.04045:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    @classmethod
    def _linear_to_srgb(cls, value):
        if value <= 0.0031308:
            return value * 12.92
        return 1.055 * (value ** (1.0 / 2.4)) - 0.055

    def _decode_color(self, rgb):
        if self._input_space == "srgb":
            return tuple(self._srgb_to_linear(channel) for channel in rgb)
        return rgb

    def _encode_color(self, rgb):
        if self._output_space == "srgb":
            return tuple(self._linear_to_srgb(channel) for channel in rgb)
        return rgb

    @staticmethod
    def _sanitize_entry(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(value):
            return 0.0
        return value

    @classmethod
    def _build_snapshot(cls, entries, size, domain, version, flip):
        try:
            numeric_size = float(size)
        except (TypeError, ValueError):
            raise LutError("LUT_INVALID_SIZE", size) from None
        if (not math.isfinite(numeric_size)
                or not numeric_size.is_integer()
                or numeric_size < 2):
            raise LutError("LUT_INVALID_SIZE", size)
        size = int(numeric_size)

        try:
            domain = (float(domain[0]), float(domain[1]))
        except (TypeError, ValueError, IndexError):
            raise LutError("LUT_INVALID_DOMAIN", domain) from None
        if (not all(math.isfinite(value) for value in domain)
                or domain[0] >= domain[1]):
            raise LutError("LUT_INVALID_DOMAIN", domain)

        try:
            raw_entries = list(entries)
        except TypeError:
            raise LutError("LUT_INVALID_ENTRIES", entries) from None

        expected = size ** 3
        first = raw_entries[0] if raw_entries else None
        nested_vector = isinstance(first, (tuple, list))
        flat_vector = len(raw_entries) == expected * 3

        if nested_vector:
            if len(raw_entries) == expected and all(
                    isinstance(item, (tuple, list)) and len(item) == 3
                    for item in raw_entries):
                converted = [
                    tuple(cls._sanitize_entry(channel) for channel in item)
                    for item in raw_entries
                ]
                vector = True
            else:
                raise LutError(
                    "LUT_SIZE_MISMATCH",
                    {"expected": expected, "actual": len(raw_entries)},
                )
        elif flat_vector:
            values = [cls._sanitize_entry(item) for item in raw_entries]
            converted = [
                tuple(values[index:index + 3])
                for index in range(0, len(values), 3)
            ]
            vector = True
        else:
            if len(raw_entries) != expected:
                raise LutError(
                    "LUT_SIZE_MISMATCH",
                    {"expected": expected, "actual": len(raw_entries)},
                )
            converted = [cls._sanitize_entry(item) for item in raw_entries]
            vector = False

        return _LutSnapshot(
            entries=tuple(converted),
            size=size,
            domain=domain,
            version=version,
            flip=bool(flip),
            vector=vector,
            padding=cls.ATLAS_PADDING,
        )

    def _install(self, snapshot):
        with self._lock:
            self._lut = snapshot
        return self

    def load_lut(self, entries, size, domain=(0.0, 1.0), version=1,
                 flip=False):
        snapshot = self._build_snapshot(entries, size, domain, version, flip)
        return self._install(snapshot)

    @staticmethod
    def _index(size, r, g, b):
        return (r * size + g) * size + b

    def _sample_snapshot(self, snapshot, rgb):
        size = snapshot.size
        low, high = snapshot.domain
        width = high - low

        coords = []
        for channel in rgb:
            normalized = (channel - low) / width
            normalized = min(1.0, max(0.0, normalized))
            coords.append(normalized)

        if snapshot.flip:
            coords[2] = 1.0 - coords[2]

        scaled = [coord * (size - 1) for coord in coords]
        lower = [min(size - 1, max(0, int(math.floor(value))))
                 for value in scaled]
        upper = [min(size - 1, value + 1) for value in lower]
        frac = [scaled[index] - lower[index] for index in range(3)]

        corners = {}
        for r_side in (0, 1):
            r = upper[0] if r_side else lower[0]
            r_weight = frac[0] if r_side else 1.0 - frac[0]
            for g_side in (0, 1):
                g = upper[1] if g_side else lower[1]
                g_weight = frac[1] if g_side else 1.0 - frac[1]
                for b_side in (0, 1):
                    b = upper[2] if b_side else lower[2]
                    b_weight = frac[2] if b_side else 1.0 - frac[2]
                    key = (r, g, b)
                    weight = r_weight * g_weight * b_weight
                    corners[key] = corners.get(key, 0.0) + weight

        if snapshot.vector:
            result = [0.0, 0.0, 0.0]
            for (r, g, b), weight in corners.items():
                entry = snapshot.entries[self._index(size, r, g, b)]
                for channel in range(3):
                    result[channel] += entry[channel] * weight
            return tuple(result)

        result = 0.0
        for (r, g, b), weight in corners.items():
            result += snapshot.entries[self._index(size, r, g, b)] * weight
        return result

    def sample(self, rgb):
        with self._lock:
            snapshot = self._lut
        if snapshot is None:
            raise LutError("LUT_NOT_LOADED")
        color = self._sanitize_color(rgb)
        return self._sample_snapshot(snapshot, color)

    def _grade_snapshot(self, snapshot, rgb, lut_version):
        if lut_version is not None and lut_version != snapshot.version:
            raise LutError(
                "LUT_VERSION_MISMATCH",
                {"requested": lut_version, "current": snapshot.version},
            )

        with self._lock:
            strength = self._strength
            exposure = self._exposure
            tone_map = self._tone_map

        color = self._decode_color(self._sanitize_color(rgb))
        working = tuple(self._sanitize_input(channel * exposure)
                        for channel in color)
        if tone_map is not None:
            mapped = tone_map(working)
            working = self._sanitize_color(mapped)

        sampled = self._sample_snapshot(snapshot, working)
        if snapshot.vector:
            graded = sampled
        else:
            graded = (sampled, sampled, sampled)

        blended = tuple(
            working[index] * (1.0 - strength) + graded[index] * strength
            for index in range(3)
        )
        encoded = self._encode_color(blended)
        return tuple(min(1.0, max(0.0, self._sanitize_input(channel)))
                     for channel in encoded)

    def grade(self, rgb, lut_version=None):
        with self._lock:
            snapshot = self._lut
        if snapshot is None:
            raise LutError("LUT_NOT_LOADED")
        return self._grade_snapshot(snapshot, rgb, lut_version)

    def set_strength(self, strength):
        strength = self._coerce_strength(strength)
        with self._lock:
            self._strength = strength
        return self

    def set_exposure(self, exposure):
        exposure = self._coerce_exposure(exposure)
        with self._lock:
            self._exposure = exposure
        return self

    def reload(self, entries, size, version, flip=False):
        with self._lock:
            domain = self._lut.domain if self._lut is not None else (0.0, 1.0)
        snapshot = self._build_snapshot(entries, size, domain, version, flip)
        return self._install(snapshot)

    def grade_backend(self, rgb, backend="d3d12"):
        if backend is None:
            raise LutError("UNKNOWN_BACKEND", backend)
        with self._lock:
            snapshot = self._lut
        if snapshot is None:
            raise LutError("LUT_NOT_LOADED")
        return self._grade_snapshot(snapshot, rgb, None)

    def render(self, colors, ui_colors=None):
        with self._lock:
            snapshot = self._lut
        if snapshot is None:
            raise LutError("LUT_NOT_LOADED")

        scene = [self._grade_snapshot(snapshot, color, None)
                 for color in colors]
        ui = [] if ui_colors is None else [
            self._sanitize_color(color) for color in ui_colors
        ]
        return {"scene": scene, "ui": ui}

    def lut_state(self):
        with self._lock:
            snapshot = self._lut
            if snapshot is None:
                return {
                    "loaded": False,
                    "version": None,
                    "size": None,
                    "domain": None,
                    "flip": False,
                    "padding": self.ATLAS_PADDING,
                    "strength": self._strength,
                    "exposure": self._exposure,
                    "input_space": self._input_space,
                    "output_space": self._output_space,
                }
            return {
                "loaded": True,
                "version": snapshot.version,
                "size": snapshot.size,
                "domain": snapshot.domain,
                "flip": snapshot.flip,
                "padding": snapshot.padding,
                "vector": snapshot.vector,
                "strength": self._strength,
                "exposure": self._exposure,
                "input_space": self._input_space,
                "output_space": self._output_space,
                "entries": snapshot.entries,
            }

    def dump(self):
        state = self.lut_state()
        if state.get("loaded"):
            state["domain"] = list(state["domain"])
            state["entries"] = [list(entry) if isinstance(entry, tuple)
                                else entry for entry in state["entries"]]
        state["format"] = 1
        return state

    def load(self, state):
        if isinstance(state, (str, bytes, bytearray)):
            try:
                state = json.loads(state)
            except (TypeError, ValueError):
                raise LutError("INVALID_STATE") from None
        if not isinstance(state, dict):
            raise LutError("INVALID_STATE", state)

        with self._lock:
            self._strength = self._coerce_strength(
                state.get("strength", self._strength))
            self._exposure = self._coerce_exposure(
                state.get("exposure", self._exposure))
            if "input_space" in state:
                self._input_space = self._coerce_space(
                    state["input_space"], "input_space")
            if "output_space" in state:
                self._output_space = self._coerce_space(
                    state["output_space"], "output_space")

        if not state.get("loaded", True):
            with self._lock:
                self._lut = None
        elif "entries" in state:
            self.load_lut(
                state["entries"],
                state.get("size", 2),
                domain=state.get("domain", (0.0, 1.0)),
                version=state.get("version", 1),
                flip=state.get("flip", False),
            )
        return self
