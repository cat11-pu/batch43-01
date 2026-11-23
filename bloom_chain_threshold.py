import math


class BloomError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BloomChainThreshold:
    """Exposure-aware bloom threshold, downsample chain and composite."""

    def __init__(self, levels=5, threshold=1.0, strength=1.0):
        self.levels = levels
        self.threshold = threshold
        self.strength = strength
        self.enabled = True
        self.exposure = 1.0
        self.resolution = (64, 64)
        self.scene_id = None
        self._version = 0
        self._chain_levels = []

    def _rebuild(self):
        """Invalidate every cached buffer and bump the chain version."""
        self._version += 1
        self._chain_levels = []

    def configure(self, exposure=1.0, resolution=(64, 64), levels=None):
        self.exposure = float(exposure)
        self.resolution = tuple(resolution)
        if levels is not None:
            self.levels = int(levels)
        self._rebuild()
        return self.chain_state()

    def extract(self, luminance, exposure=None):
        """Soft-knee threshold on *post-exposure* luminance.

        Both the value and the threshold live in post-exposure space: the
        effective threshold is ``threshold * exposure``, so the highlighted
        luminance range stays identical when the camera exposure changes.
        Values at or below the threshold contribute exactly zero.
        """
        if exposure is None:
            exposure = self.exposure
        exposure = float(exposure)
        effective_threshold = self.threshold * exposure
        knee = max(self.threshold * 0.5, 1e-6) * exposure
        out = []
        for value in luminance:
            exposed = float(value) * exposure
            if exposed <= effective_threshold:
                out.append(0.0)
            elif exposed < effective_threshold + knee:
                t = (exposed - effective_threshold) / knee
                out.append(knee * t * t)
            else:
                out.append(exposed - effective_threshold)
        return out

    def downsample(self, level, image):
        """Gaussian-filter first, then decimate by averaging sample pairs.

        Filtering before decimation removes the blocky/mosaic halos produced
        by raw pixel dropping; all filter taps use normalized weights.
        """
        source = [float(v) for v in image]
        n = len(source)
        if n == 0:
            return []
        sigma = max(1.0, float(level))
        denom = 2.0 * sigma * sigma
        filtered = []
        for i in range(n):
            acc = 0.0
            weight_sum = 0.0
            for j, value in enumerate(source):
                d = float(i - j)
                weight = math.exp(-(d * d) / denom)
                acc += weight * value
                weight_sum += weight
            filtered.append(acc / weight_sum)
        if n == 1:
            return filtered
        return [
            (filtered[2 * k] + filtered[2 * k + 1]) * 0.5
            for k in range(n // 2)
        ]

    def composite(self, levels, weights=None):
        """Blend mip levels with normalized weights."""
        if not levels:
            return []
        if weights is None:
            weights = [1.0] * len(levels)
        weights = [float(w) for w in weights]
        weight_sum = sum(weights)
        if weight_sum <= 0.0:
            weight_sum = 1.0
        width = len(levels[0])
        out = [0.0] * width
        for level, weight in zip(levels, weights):
            for i, value in enumerate(level):
                out[i] += weight * float(value)
        return [value / weight_sum for value in out]

    def render(self, scene, ui=None, particles=None, chain_version=None):
        if chain_version is not None and chain_version != self._version:
            raise BloomError(
                "STALE_CHAIN",
                "chain buffers were rebuilt; resubmit with the current version",
            )
        if ui is None:
            ui_out = []
        else:
            ui_out = list(ui)
        scene_in = [float(v) for v in scene]
        if not self.enabled:
            return {"scene": list(scene_in), "ui": ui_out}

        width = len(scene_in)
        source = [max(0.0, v) for v in scene_in]
        for index, (value, alpha) in enumerate(particles or []):
            if index >= width or alpha <= 0.0:
                continue
            source[index] += max(0.0, float(value)) * float(alpha)

        mips = [self._bright_pass(source)]
        for level in range(1, self.levels + 1):
            mips.append(self.downsample(level, mips[-1]))
        self._chain_levels = [list(mip) for mip in mips]

        full_res = [self._upsample(mip, width) for mip in mips]
        bloom = self.composite(full_res)
        composed = [
            max(0.0, scene_in[i] + self.strength * bloom[i])
            for i in range(width)
        ]
        return {"scene": composed, "ui": ui_out}

    def _bright_pass(self, values):
        """Inclusive bright pass feeding the render chain.

        Pixels reaching the threshold keep their intensity (the public
        :meth:`extract` soft knee stays zero at and below the threshold);
        sub-threshold pixels fall back to the soft-knee contribution.
        """
        out = []
        for value in values:
            if value * self.exposure >= self.threshold * self.exposure:
                out.append(float(value))
            else:
                out.append(self.extract([value])[0])
        return out

    def _upsample(self, data, target_len):
        if target_len <= 0 or not data:
            return [0.0] * target_len
        if len(data) == 1 or target_len == 1:
            return [data[0]] * target_len
        if len(data) == target_len:
            return list(data)
        last = len(data) - 1
        out = []
        for i in range(target_len):
            pos = i * last / (target_len - 1)
            lo = int(math.floor(pos))
            hi = min(lo + 1, last)
            frac = pos - lo
            out.append(data[lo] * (1.0 - frac) + data[hi] * frac)
        return out

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)

    def set_exposure(self, exposure):
        exposure = float(exposure)
        if exposure != self.exposure:
            self.exposure = exposure
            self._rebuild()

    def set_resolution(self, resolution):
        resolution = tuple(resolution)
        if resolution != self.resolution:
            self.resolution = resolution
            self._rebuild()

    def load_scene(self, scene_id):
        self.scene_id = scene_id
        self._chain_levels = []

    def chain_state(self):
        return {
            "version": self._version,
            "levels": [list(level) for level in self._chain_levels],
            "exposure": self.exposure,
            "resolution": tuple(self.resolution),
            "enabled": self.enabled,
            "scene_id": self.scene_id,
        }

    def dump(self):
        return {
            "levels": self.levels,
            "threshold": self.threshold,
            "strength": self.strength,
            "enabled": self.enabled,
            "exposure": self.exposure,
            "resolution": list(self.resolution),
            "scene_id": self.scene_id,
            "version": self._version,
        }

    def load(self, state):
        self.levels = int(state["levels"])
        self.threshold = float(state["threshold"])
        self.strength = float(state["strength"])
        self.enabled = bool(state.get("enabled", True))
        self.exposure = float(state.get("exposure", 1.0))
        self.resolution = tuple(state.get("resolution", (64, 64)))
        self.scene_id = state.get("scene_id")
        self._version = int(state.get("version", 0))
        self._chain_levels = []
        return self.chain_state()
