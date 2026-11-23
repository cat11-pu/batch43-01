import math
import threading


class GovernorError(ValueError):
    """Raised when a resolution budget request is invalid."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ResolutionBudgetGovernor:
    """Hysteresis based dynamic resolution with native UI and screenshots."""

    MIN_SAMPLES = 5
    DOWNSCALE_RATIO = 1.10
    UPSCALE_RATIO = 0.85
    UPSCALE_WINDOWS = 2

    _DEFAULT_BUFFERS = ("color", "depth", "post")
    _MAIN_VIEWS = {"main", "main_view", "primary", "gameplay"}

    _THERMAL_LEVELS = {
        "none": (1.0, 1.0),
        "normal": (1.0, 1.0),
        "nominal": (1.0, 1.0),
        "cool": (1.0, 1.0),
        "low": (1.0, 1.0),
        "fair": (0.7, 0.7),
        "warm": (0.7, 0.7),
        "medium": (0.7, 0.7),
        "moderate": (0.7, 0.7),
        "high": (0.7, 0.7),
        "serious": (0.7, 0.7),
        "severe": (0.5, 0.5),
        "critical": (0.5, 0.5),
        "throttled": (0.5, 0.5),
        "emergency": (0.5, 0.5),
    }

    def __init__(self, scales=(0.5, 0.7, 1.0), target_fps=60,
                 native=(1920, 1080)):
        normalized = []
        for value in scales:
            try:
                scale = float(value)
            except (TypeError, ValueError) as exc:
                raise GovernorError("invalid_scale", value) from exc
            if not math.isfinite(scale) or scale <= 0.0 or scale > 1.0:
                raise GovernorError("invalid_scale", value)
            if scale not in normalized:
                normalized.append(scale)
        if not normalized:
            raise GovernorError("invalid_scale", "empty scale list")

        try:
            target_fps = float(target_fps)
        except (TypeError, ValueError) as exc:
            raise GovernorError("invalid_target_fps", target_fps) from exc
        if not math.isfinite(target_fps) or target_fps <= 0.0:
            raise GovernorError("invalid_target_fps", target_fps)

        if len(native) != 2:
            raise GovernorError("invalid_native_size", native)
        native_width, native_height = (int(v) for v in native)
        if native_width <= 0 or native_height <= 0:
            raise GovernorError("invalid_native_size", native)

        self.scales = sorted(normalized)
        self.target_fps = target_fps
        self.native = (native_width, native_height)
        self._frame_budget_ms = 1000.0 / target_fps

        self._scale = max(self.scales)
        self._generation = 0
        self._buffer_bases = {
            name: self.native for name in self._DEFAULT_BUFFERS
        }
        self._buffers = {}
        self._rebuild_buffers_locked(self._scale)

        self._samples = []
        self._gpu_presence = []
        self._gpu_timing = "missing"
        self._up_windows = 0
        self._needs_evaluation = True
        self._background = False
        self._thermal = "normal"
        self._thermal_cap = 1.0
        self._lock = threading.RLock()

    def sample(self, frame_time_ms=None, gpu_ms=None, cpu_ms=None,
               view="main"):
        frame_time = self._validated_time(frame_time_ms, "frame_time_ms")
        gpu_time = self._validated_time(gpu_ms, "gpu_ms")
        cpu_time = self._validated_time(cpu_ms, "cpu_ms")

        with self._lock:
            if self._background:
                return {
                    "accepted": False,
                    "reason": "background",
                    "view": view,
                    "gpu_timing": self._gpu_timing,
                }

            view_name = str(view).strip().lower()
            if view_name not in self._MAIN_VIEWS:
                return {
                    "accepted": False,
                    "reason": "non_main_view",
                    "view": view,
                    "gpu_timing": self._gpu_timing,
                }

            if frame_time is None and gpu_time is None and cpu_time is None:
                estimated_frame_time = self._frame_budget_ms
            else:
                candidates = [
                    value for value in (frame_time, gpu_time, cpu_time)
                    if value is not None
                ]
                estimated_frame_time = max(candidates)

            self._samples.append(estimated_frame_time)
            self._gpu_presence.append(gpu_time is not None)
            if all(self._gpu_presence):
                self._gpu_timing = "present"
            elif any(self._gpu_presence):
                self._gpu_timing = "partial"
            else:
                self._gpu_timing = "missing"
            self._needs_evaluation = True

            return {
                "accepted": True,
                "view": view,
                "frame_time_ms": estimated_frame_time,
                "gpu_timing": self._gpu_timing,
            }

    def decide(self):
        with self._lock:
            if self._background:
                return self._decision(False, "background")

            current_index = self._scale_index(self._scale)
            thermal_index = self._thermal_index()
            if current_index > thermal_index:
                self._samples.clear()
                self._gpu_presence.clear()
                self._up_windows = 0
                new_scale = self.scales[current_index - 1]
                result = self.apply(new_scale)
                self._needs_evaluation = new_scale != self.scales[thermal_index]
                return self._changed_decision(result, "thermal_limit")

            if len(self._samples) < self.MIN_SAMPLES:
                self._samples.clear()
                self._gpu_presence.clear()
                self._up_windows = 0
                return self._decision(False, "insufficient_samples")

            samples = sorted(self._samples)
            middle = len(samples) // 2
            if len(samples) % 2:
                typical_ms = samples[middle]
            else:
                typical_ms = (samples[middle - 1] + samples[middle]) / 2.0

            self._samples.clear()
            self._gpu_presence.clear()
            self._needs_evaluation = False

            down_threshold = self._frame_budget_ms * self.DOWNSCALE_RATIO
            up_threshold = self._frame_budget_ms * self.UPSCALE_RATIO

            if typical_ms >= down_threshold:
                self._up_windows = 0
                if current_index == 0:
                    return self._decision(False, "at_minimum")
                result = self.apply(self.scales[current_index - 1])
                return self._changed_decision(result, "gpu_or_frame_budget")

            if typical_ms <= up_threshold:
                if current_index >= len(self.scales) - 1:
                    self._up_windows = 0
                    return self._decision(False, "at_maximum")
                self._up_windows += 1
                if self._up_windows < self.UPSCALE_WINDOWS:
                    return self._decision(False, "upscale_hysteresis")
                self._up_windows = 0
                target_index = min(current_index + 1, thermal_index)
                if target_index <= current_index:
                    return self._decision(False, "thermal_limit")
                result = self.apply(self.scales[target_index])
                return self._changed_decision(result, "sustained_headroom")

            self._up_windows = 0
            return self._decision(False, "inside_hysteresis_band")

    def apply(self, scale):
        try:
            normalized = float(scale)
        except (TypeError, ValueError) as exc:
            raise GovernorError("invalid_scale", scale) from exc

        with self._lock:
            if normalized not in self.scales:
                raise GovernorError("invalid_scale", scale)

            if normalized == self._scale:
                return {
                    "changed": False,
                    "scale": self._scale,
                    "generation": self._generation,
                    "width": self.render_size()[0],
                    "height": self.render_size()[1],
                }

            self._scale = normalized
            self._samples.clear()
            self._gpu_presence.clear()
            self._up_windows = 0
            self._generation += 1
            self._rebuild_buffers_locked(normalized)
            return {
                "changed": True,
                "scale": self._scale,
                "generation": self._generation,
                "width": self.render_size()[0],
                "height": self.render_size()[1],
            }

    def reset(self):
        with self._lock:
            self._scale = self.scales[-1]
            self._samples.clear()
            self._gpu_presence.clear()
            self._gpu_timing = "missing"
            self._up_windows = 0
            self._needs_evaluation = True
            self._background = False
            self._thermal = "normal"
            self._thermal_cap = 1.0
            self._generation += 1
            self._rebuild_buffers_locked(self._scale)
            return self.state()

    def state(self):
        with self._lock:
            return {
                "scale": self._scale,
                "samples": len(self._samples),
                "gpu_timing": self._gpu_timing,
                "target_fps": self.target_fps,
                "native": self.native,
                "render_size": self.render_size(),
                "thermal": self._thermal,
                "thermal_cap": self._thermal_cap,
                "background": self._background,
                "needs_evaluation": self._needs_evaluation,
                "generation": self._generation,
            }

    def scale(self):
        with self._lock:
            return self._scale

    def ui_size(self):
        return self.native

    def render_size(self):
        with self._lock:
            return self._scaled_size(self.native, self._scale)

    def capture(self, kind="screenshot"):
        with self._lock:
            return {
                "kind": kind,
                "width": self.native[0],
                "height": self.native[1],
                "native": True,
            }

    def buffers(self):
        with self._lock:
            return {
                name: dict(size) for name, size in self._buffers.items()
            }

    def register_buffer(self, name, width, height):
        if not isinstance(name, str) or not name:
            raise GovernorError("invalid_buffer", name)
        width = self._validated_dimension(width, "width")
        height = self._validated_dimension(height, "height")

        with self._lock:
            self._buffer_bases[name] = (width, height)
            size = self._scaled_size((width, height), self._scale)
            self._buffers[name] = {
                "width": size[0],
                "height": size[1],
            }
            return dict(self._buffers[name])

    def late_callback(self, generation, name, width, height):
        with self._lock:
            if generation != self._generation:
                return {
                    "status": "ignored",
                    "reason": "stale_generation",
                    "generation": self._generation,
                }
            if name not in self._buffers:
                return {
                    "status": "ignored",
                    "reason": "unknown_buffer",
                    "generation": self._generation,
                }
            width = self._validated_dimension(width, "width")
            height = self._validated_dimension(height, "height")
            self._buffers[name] = {
                "width": width,
                "height": height,
            }
            return {
                "status": "applied",
                "generation": self._generation,
                "name": name,
                "width": width,
                "height": height,
            }

    def enter_background(self):
        with self._lock:
            self._background = True
            self._samples.clear()
            self._gpu_presence.clear()
            self._up_windows = 0
            self._needs_evaluation = True
            return self.state()

    def resume(self):
        with self._lock:
            self._background = False
            self._samples.clear()
            self._gpu_presence.clear()
            self._up_windows = 0
            self._needs_evaluation = True
            return self.state()

    def set_thermal(self, level):
        normalized = str(level).strip().lower()
        if normalized in self._THERMAL_LEVELS:
            name, thermal_cap = normalized, self._THERMAL_LEVELS[normalized][1]
        else:
            try:
                numeric_level = int(normalized)
            except (TypeError, ValueError) as exc:
                raise GovernorError("invalid_thermal_level", level) from exc
            numeric_levels = {
                0: ("normal", 1.0),
                1: ("medium", 0.7),
                2: ("high", 0.7),
                3: ("critical", 0.5),
            }
            if numeric_level not in numeric_levels:
                raise GovernorError("invalid_thermal_level", level)
            name, thermal_cap = numeric_levels[numeric_level]

        with self._lock:
            self._thermal = name
            self._thermal_cap = thermal_cap
            self._samples.clear()
            self._gpu_presence.clear()
            self._up_windows = 0
            self._needs_evaluation = True
            return self.state()

    @staticmethod
    def _validated_time(value, field):
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise GovernorError("invalid_sample", {field: value}) from exc
        if not math.isfinite(number) or number < 0.0:
            raise GovernorError("invalid_sample", {field: value})
        return number

    @staticmethod
    def _validated_dimension(value, field):
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise GovernorError("invalid_buffer", {field: value}) from exc
        if number <= 0:
            raise GovernorError("invalid_buffer", {field: value})
        return number

    @staticmethod
    def _scaled_size(size, scale):
        width = max(1, int(size[0] * scale + 0.5))
        height = max(1, int(size[1] * scale + 0.5))
        return (width, height)

    def _scale_index(self, scale):
        return self.scales.index(scale)

    def _thermal_index(self):
        eligible = [
            index for index, scale in enumerate(self.scales)
            if scale <= self._thermal_cap + 1e-9
        ]
        return eligible[-1] if eligible else 0

    def _rebuild_buffers_locked(self, scale):
        rebuilt = {}
        for name, base_size in self._buffer_bases.items():
            size = self._scaled_size(base_size, scale)
            rebuilt[name] = {
                "width": size[0],
                "height": size[1],
            }
        self._buffers = rebuilt

    def _decision(self, changed, reason):
        width, height = self.render_size()
        return {
            "changed": changed,
            "reason": reason,
            "scale": self._scale,
            "generation": self._generation,
            "width": width,
            "height": height,
        }

    def _changed_decision(self, applied, reason):
        return {
            "changed": applied["changed"],
            "reason": reason,
            "scale": self._scale,
            "generation": self._generation,
            "width": applied["width"],
            "height": applied["height"],
        }
