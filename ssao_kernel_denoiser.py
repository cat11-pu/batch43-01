import math


class SsaoError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _clamp(value, low, high):
    return low if value < low else high if value > high else value


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(v):
    return math.sqrt(_dot(v, v))


def _normalize(v):
    length = _length(v)
    if length <= 1e-12:
        return None
    return (v[0] / length, v[1] / length, v[2] / length)


def _subtract(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _copy_grid(grid):
    return [list(row) for row in grid]


def _grid_size(grid):
    if not isinstance(grid, (list, tuple)) or not grid:
        return (0, 0)
    width = len(grid[0])
    if width == 0 or any(not isinstance(row, (list, tuple)) or
                         len(row) != width for row in grid):
        return (0, 0)
    return (len(grid), width)


class SsaoKernelDenoiser:
    """Screen-space ambient occlusion over a coherent camera snapshot.

    Depth values are linear view-space distances.  A frame is visible to
    :meth:`generate` only after both the depth and normal buffers for the same
    version have been published with :meth:`swap_buffers`.
    """

    _GOLDEN_ANGLE = 2.399963229728653

    def __init__(self, radius=1.0, strength=1.0, min_ambient=0.2,
                 sample_count=8, denoise=True, enabled=True):
        if radius < 0.0:
            raise SsaoError("INVALID_ARGUMENT",
                            "radius must be non-negative")
        if strength < 0.0:
            raise SsaoError("INVALID_ARGUMENT", "strength must be non-negative")
        if sample_count < 0:
            raise SsaoError("INVALID_ARGUMENT",
                            "sample_count must be non-negative")

        self.radius = float(radius)
        self.strength = float(strength)
        self.min_ambient = _clamp(float(min_ambient), 0.0, 1.0)
        self.sample_count = int(sample_count)
        self.denoise = bool(denoise)
        self.enabled = bool(enabled)

        self._camera = {
            "near": 0.1,
            "far": 100.0,
            "fov": 1.0,
            "resolution": (4, 4),
        }
        self.near = self._camera["near"]
        self.far = self._camera["far"]
        self.fov = self._camera["fov"]
        self.resolution = self._camera["resolution"]

        self._kernel_cache = None
        self._kernel_key = None

        # Staging buffers are deliberately separate from the published
        # snapshot so a renderer can never observe a mixed pair.
        self._pending_depth = None
        self._pending_normals = None
        self._published = None

        self._sky = None
        self._transparent = None

    # ------------------------------------------------------------------
    # Camera and snapshot setup
    # ------------------------------------------------------------------
    def set_camera(self, near=0.1, far=100.0, fov=1.0, resolution=(4, 4)):
        near = float(near)
        far = float(far)
        fov = float(fov)
        width, height = self._validate_resolution(resolution)

        if not math.isfinite(near) or near <= 0.0:
            raise SsaoError("INVALID_CAMERA", "near plane must be positive")
        if not math.isfinite(far) or far <= near:
            raise SsaoError("INVALID_CAMERA",
                            "far plane must be greater than near plane")
        if not math.isfinite(fov) or not 0.0 < fov < math.pi:
            raise SsaoError("INVALID_CAMERA", "fov must be in (0, pi)")

        old_resolution = self._camera["resolution"]
        self._camera = {
            "near": near,
            "far": far,
            "fov": fov,
            "resolution": (width, height),
        }
        self.near = near
        self.far = far
        self.fov = fov
        self.resolution = (width, height)
        self._kernel_cache = None
        self._kernel_key = None

        if old_resolution != (width, height):
            self._invalidate_buffers()
        return self

    def set_resolution(self, resolution):
        width, height = self._validate_resolution(resolution)
        if (width, height) == self._camera["resolution"]:
            return self

        # The kernel is expressed in camera/world units, so changing the
        # raster size must not rebuild it.  The old depth/normal pair is no
        # longer valid at the new size, however.
        self._camera["resolution"] = (width, height)
        self.resolution = (width, height)
        self._invalidate_buffers()
        return self

    @staticmethod
    def _validate_resolution(resolution):
        if (not isinstance(resolution, (list, tuple)) or
                len(resolution) != 2):
            raise SsaoError("INVALID_CAMERA", "resolution must be (width, height)")
        try:
            width = int(resolution[0])
            height = int(resolution[1])
        except (TypeError, ValueError):
            raise SsaoError("INVALID_CAMERA",
                            "resolution must contain integers") from None
        if width <= 0 or height <= 0:
            raise SsaoError("INVALID_CAMERA",
                            "resolution dimensions must be positive")
        return width, height

    def _invalidate_buffers(self):
        self._pending_depth = None
        self._pending_normals = None
        self._published = None
        self._sky = None
        self._transparent = None

    def set_depth(self, depth, version=1):
        rows, columns = _grid_size(depth)
        if rows == 0 or columns == 0:
            raise SsaoError("INVALID_BUFFER", "depth must be a non-empty grid")
        self._pending_depth = (_copy_grid(depth), version)
        return self

    def set_normals(self, normals, version=1):
        rows, columns = _grid_size(normals)
        if rows == 0 or columns == 0:
            raise SsaoError("INVALID_BUFFER", "normals must be a non-empty grid")
        for row in normals:
            for normal in row:
                if not isinstance(normal, (list, tuple)) or len(normal) != 3:
                    raise SsaoError("INVALID_BUFFER",
                                    "normals must contain 3-component vectors")
        self._pending_normals = (_copy_grid(normals), version)
        return self

    def swap_buffers(self, version):
        if self._pending_depth is None and self._pending_normals is None:
            raise SsaoError("BUFFER_NOT_READY",
                            "no buffers have been staged")

        depth_stage = self._pending_depth
        normal_stage = self._pending_normals
        staged_versions = [
            stage[1] for stage in (depth_stage, normal_stage)
            if stage is not None
        ]
        if any(staged_version != version
               for staged_version in staged_versions):
            raise SsaoError("STALE_BUFFER",
                            "depth, normals and frame versions must match")
        if (depth_stage is not None and normal_stage is not None and
                depth_stage[1] != normal_stage[1]):
            raise SsaoError("STALE_BUFFER",
                            "depth and normals must come from the same frame")

        # A half-resolution AO pass is valid, so the source buffers need not
        # match the camera raster.  An incomplete pair may be published only
        # when there is no older complete snapshot; generate() then refuses to
        # render it instead of mixing a new depth buffer with old normals.
        if (depth_stage is None or normal_stage is None) and \
                self._published is not None:
            return self

        # Publish the whole camera/depth/normal snapshot in one assignment.
        self._published = {
            "version": version,
            "camera": dict(self._camera),
            "depth": _copy_grid(depth_stage[0]) if depth_stage else None,
            "normals": _copy_grid(normal_stage[0]) if normal_stage else None,
        }
        return self

    def set_sky(self, mask):
        self._sky = self._validate_mask(mask)
        return self

    def set_transparent(self, mask):
        self._transparent = self._validate_mask(mask)
        return self

    def _validate_mask(self, mask):
        if mask is None:
            return None
        rows, columns = _grid_size(mask)
        if rows == 0 or columns == 0:
            raise SsaoError("INVALID_BUFFER", "mask must be a non-empty grid")
        return [[bool(value) for value in row] for row in mask]

    # ------------------------------------------------------------------
    # Kernel and quality
    # ------------------------------------------------------------------
    def sample_kernel(self):
        key = (
            self.sample_count,
            self._camera["near"],
            self._camera["far"],
            self._camera["fov"],
        )
        if self._kernel_cache is not None and self._kernel_key == key:
            return list(self._kernel_cache)

        kernel = self._sample_kernel_for(self._camera)
        self._kernel_cache = tuple(kernel)
        self._kernel_key = key
        return list(self._kernel_cache)

    def _sample_kernel_for(self, camera):
        count = self.sample_count
        if count == 0:
            return ()
        near = camera["near"]
        far = camera["far"]
        fov = camera["fov"]
        phase = (fov + near / max(far, near)) * self._GOLDEN_ANGLE
        spread = math.tan(fov * 0.5)
        kernel = []
        for index in range(count):
            # Stratified hemisphere samples in view space.  The radius is
            # applied later in world units, so resolution never enters the
            # kernel distribution.
            z = (index + 0.5) / count
            radial = math.sqrt(max(0.0, 1.0 - z * z))
            angle = index * self._GOLDEN_ANGLE + phase
            vector = (
                radial * math.cos(angle) * spread,
                radial * math.sin(angle) * spread,
                z,
            )
            unit = _normalize(vector) or (0.0, 0.0, 1.0)
            kernel.append(unit)
        return tuple(kernel)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        return self

    def set_quality(self, quality):
        if quality in (False, 0, "off", "disabled", None):
            self.enabled = False
        elif quality in (1, "low", "mobile"):
            self.enabled = True
            self.sample_count = min(self.sample_count, 4) if self.sample_count else 4
            self.denoise = False
        elif quality in (2, "medium", "balanced"):
            self.enabled = True
            self.sample_count = 8
            self.denoise = True
        elif quality in (3, "high"):
            self.enabled = True
            self.sample_count = 16
            self.denoise = True
        else:
            raise SsaoError("INVALID_ARGUMENT", "unknown quality level")
        return self

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    def generate(self, frame_version=None):
        if not self.enabled:
            width, height = self._camera["resolution"]
            version = self._published["version"] if self._published else frame_version
            return {
                "occlusion": [[0.0 for _ in range(width)] for _ in range(height)],
                "version": version,
            }

        snapshot = self._published
        if snapshot is None:
            raise SsaoError("BUFFER_NOT_READY",
                            "call swap_buffers before generating occlusion")
        if frame_version is not None and frame_version != snapshot["version"]:
            raise SsaoError("STALE_BUFFER",
                            "requested frame does not match the published buffer")

        camera = snapshot["camera"]
        width, height = camera["resolution"]
        if snapshot["depth"] is None or snapshot["normals"] is None:
            raise SsaoError("BUFFER_NOT_READY",
                            "published frame is missing depth or normals")
        depth = self._resample_numeric(snapshot["depth"], height, width)
        normals = self._resample_grid(snapshot["normals"], height, width)

        sky = (self._resample_mask(self._sky, height, width)
               if self._sky is not None else None)
        transparent = (self._resample_mask(self._transparent, height, width)
                       if self._transparent is not None else None)

        occlusion = [[0.0 for _ in range(width)] for _ in range(height)]
        candidates = [[False for _ in range(width)] for _ in range(height)]
        taps = self._screen_taps(camera, depth)

        for row in range(height):
            for column in range(width):
                if not self._is_surface(depth, sky, transparent, row, column,
                                        camera):
                    continue

                normal = self._surface_normal(normals, depth, camera,
                                              row, column)
                if normal is None:
                    continue

                center_depth = float(depth[row][column])
                position = self._view_position(column, row, center_depth,
                                               camera)
                bias = max(1e-5, min(1e-3, center_depth * 1e-4))
                attenuation = self._distance_attenuation(center_depth, camera)
                denominator = 0.0
                total = 0.0

                for delta_x, delta_y, tap_weight in taps:
                    sample_row = row + delta_y
                    sample_column = column + delta_x
                    if not (0 <= sample_row < height and
                            0 <= sample_column < width):
                        continue
                    if not self._is_surface(depth, sky, transparent,
                                            sample_row, sample_column, camera):
                        continue

                    denominator += tap_weight
                    neighbor_depth = float(depth[sample_row][sample_column])
                    delta_depth = center_depth - neighbor_depth
                    if delta_depth <= bias:
                        continue

                    if self.radius > 0.0:
                        influence = 1.0 - delta_depth / self.radius
                    else:
                        influence = 0.0
                    if influence <= 0.0:
                        continue

                    neighbor_position = self._view_position(
                        sample_column, sample_row, neighbor_depth, camera
                    )
                    direction = _subtract(neighbor_position, position)
                    if _dot(direction, normal) <= 0.0:
                        continue

                    candidates[row][column] = True
                    total += tap_weight * influence * attenuation

                if denominator > 0.0:
                    occlusion[row][column] = _clamp(
                        self.strength * total / denominator, 0.0, 1.0
                    )

        if self.denoise:
            occlusion = self._depth_aware_denoise(
                occlusion, candidates, depth, normals, sky, transparent, camera
            )

        return {"occlusion": occlusion, "version": snapshot["version"]}

    def _distance_attenuation(self, depth, camera):
        # Keep contact AO near the camera, but fade a fixed screen-space
        # radius as the surface recedes.  This prevents distant geometry from
        # receiving the same wash as foreground geometry.
        falloff = max(self.radius * 4.0, camera["near"])
        ratio = depth / falloff
        return 1.0 / (1.0 + ratio * ratio)

    def _screen_taps(self, camera, depth):
        height = camera["resolution"][1]
        tan_half = math.tan(camera["fov"] * 0.5)
        focal_pixels = (height * 0.5) / max(tan_half, 1e-6)

        # The tap radius follows the same camera snapshot as the sampling
        # kernel.  A one-pixel minimum keeps far geometry sampleable without
        # making the world-space radius resolution dependent.
        depths = []
        for row in depth:
            for value in row:
                try:
                    depth_value = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(depth_value) and depth_value > 0.0:
                    depths.append(depth_value)
        reference_depth = (
            sum(depths) / len(depths)
            if depths else camera["near"]
        )
        pixel_radius = self.radius * focal_pixels / max(reference_depth,
                                                        camera["near"])
        max_ring = int(_clamp(math.ceil(pixel_radius), 1, 4))

        taps = []
        ring = 1
        while ring <= max_ring and len(taps) < self.sample_count:
            weight = 1.0 / (ring * ring)
            for delta_x, delta_y in ((0, -ring), (ring, 0),
                                     (0, ring), (-ring, 0)):
                taps.append((delta_x, delta_y, weight))
                if len(taps) >= self.sample_count:
                    break
            ring += 1

        # If the camera is very close, add diagonal samples at radius two or
        # more.  A radius-one diagonal would incorrectly turn a single closer
        # neighbour into a corner and is intentionally avoided.
        ring = 2
        while len(taps) < self.sample_count and ring <= max_ring + 1:
            weight = 0.5 / (ring * ring)
            for delta_x, delta_y in ((ring, ring), (ring, -ring),
                                     (-ring, ring), (-ring, -ring)):
                taps.append((delta_x, delta_y, weight))
                if len(taps) >= self.sample_count:
                    break
            ring += 1

        # Modulate the screen taps with the same camera-snapshot kernel used
        # by the public kernel query.  This keeps fov/near/far changes
        # coherent with the actual AO result without tying tap positions to
        # the raster resolution.
        kernel = self._sample_kernel_for(camera)
        if not kernel:
            return []
        weighted_taps = []
        for index, (delta_x, delta_y, weight) in enumerate(taps):
            directional = 0.5 + 0.5 * abs(kernel[index % len(kernel)][0])
            weighted_taps.append((delta_x, delta_y, weight * directional))
        return weighted_taps

    @staticmethod
    def _resample_grid(grid, out_height, out_width):
        in_height = len(grid)
        in_width = len(grid[0])
        if (in_height, in_width) == (out_height, out_width):
            return _copy_grid(grid)
        return [
            [
                grid[min(in_height - 1,
                         int((row + 0.5) * in_height / out_height))]
                    [min(in_width - 1,
                         int((column + 0.5) * in_width / out_width))]
                for column in range(out_width)
            ]
            for row in range(out_height)
        ]

    @classmethod
    def _resample_numeric(cls, grid, out_height, out_width):
        resampled = cls._resample_grid(grid, out_height, out_width)
        result = []
        for row in resampled:
            converted = []
            for value in row:
                try:
                    converted.append(float(value))
                except (TypeError, ValueError):
                    converted.append(float("nan"))
            result.append(converted)
        return result

    @classmethod
    def _resample_mask(cls, mask, out_height, out_width):
        resampled = cls._resample_grid(mask, out_height, out_width)
        return [[bool(value) for value in row] for row in resampled]

    def _is_surface(self, depth, sky, transparent, row, column, camera):
        if sky is not None and sky[row][column]:
            return False
        if transparent is not None and transparent[row][column]:
            return False
        try:
            value = float(depth[row][column])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value <= 0.0:
            return False
        if value >= camera["far"]:
            return False
        return True

    def _view_position(self, column, row, depth, camera):
        width, height = camera["resolution"]
        ndc_x = ((column + 0.5) / width) * 2.0 - 1.0
        ndc_y = 1.0 - ((row + 0.5) / height) * 2.0
        tan_half = math.tan(camera["fov"] * 0.5)
        aspect = width / float(height)
        return (
            ndc_x * tan_half * aspect * depth,
            ndc_y * tan_half * depth,
            -depth,
        )

    def _surface_normal(self, normals, depth, camera, row, column):
        normal = normals[row][column]
        try:
            unit = _normalize((float(normal[0]), float(normal[1]),
                               float(normal[2])))
        except (TypeError, ValueError, IndexError):
            return None
        if unit is None:
            return None

        position = self._view_position(
            column, row, float(depth[row][column]), camera
        )
        to_camera = _normalize((-position[0], -position[1], -position[2]))
        if to_camera is not None and _dot(unit, to_camera) < 0.0:
            unit = (-unit[0], -unit[1], -unit[2])
        return unit

    def _depth_aware_denoise(self, occlusion, candidates, depth, normals,
                             sky, transparent, camera):
        width, height = camera["resolution"]
        result = [[0.0 for _ in range(width)] for _ in range(height)]
        offsets = ((0, 0, 2.0), (0, -1, 1.0), (1, 0, 1.0),
                   (0, 1, 1.0), (-1, 0, 1.0))

        for row in range(height):
            for column in range(width):
                if not candidates[row][column]:
                    # Never introduce AO at a pixel which cannot receive a
                    # valid visible-depth sample.
                    continue
                if not self._is_surface(depth, sky, transparent, row, column,
                                        camera):
                    continue

                center_depth = float(depth[row][column])
                center_normal = self._surface_normal(
                    normals, depth, camera, row, column
                )
                if center_normal is None:
                    continue
                depth_tolerance = max(
                    self.radius * 0.05,
                    center_depth * 0.02,
                    max(1e-5, min(1e-3, center_depth * 1e-4)) * 4.0,
                )

                weighted = 0.0
                weight_sum = 0.0
                for delta_x, delta_y, spatial in offsets:
                    sample_row = row + delta_y
                    sample_column = column + delta_x
                    if not (0 <= sample_row < height and
                            0 <= sample_column < width):
                        continue
                    if not candidates[sample_row][sample_column]:
                        continue
                    if not self._is_surface(depth, sky, transparent,
                                            sample_row, sample_column, camera):
                        continue
                    if abs(float(depth[sample_row][sample_column]) -
                           center_depth) > depth_tolerance:
                        continue
                    sample_normal = self._surface_normal(
                        normals, depth, camera, sample_row, sample_column
                    )
                    if sample_normal is None:
                        continue
                    if _dot(center_normal, sample_normal) < 0.9:
                        continue
                    weighted += occlusion[sample_row][sample_column] * spatial
                    weight_sum += spatial

                if weight_sum > 0.0:
                    filtered = weighted / weight_sum
                    result[row][column] = _clamp(
                        occlusion[row][column] * 0.65 + filtered * 0.35,
                        0.0, 1.0,
                    )
        return result

    # ------------------------------------------------------------------
    # Lighting
    # ------------------------------------------------------------------
    def shade(self, occlusion, intensity=1.0):
        """Return the multiplicative indirect-light factor for occlusion."""
        try:
            value = float(occlusion)
            level = float(intensity)
        except (TypeError, ValueError):
            raise SsaoError("INVALID_ARGUMENT",
                            "occlusion and intensity must be numeric") from None
        value = _clamp(value, 0.0, 1.0)
        level = max(0.0, level)
        factor = 1.0 - level * value
        return _clamp(factor, self.min_ambient, 1.0)

    def apply_lighting(self, direct, indirect, occlusion, intensity=1.0):
        """Apply AO only to indirect light."""
        return direct + indirect * self.shade(occlusion, intensity)
