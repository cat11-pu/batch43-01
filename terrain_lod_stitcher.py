import copy
import math


class TerrainError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TerrainLodStitcher:
    """Terrain chunk LOD meshes, border stitching and physics heights.

    The renderer, physics sampler and texture blend all use the same immutable
    heightmap snapshot that was current when a chunk was built. Chunk borders
    are always sampled on the finest global grid, independent of the interior
    LOD, which makes neighbouring meshes share exactly the same edge vertices.
    """

    _UINT16_MAX_VERTICES = 65535

    def __init__(self, chunk_size=4, split_distance=32.0,
                 height_epsilon=0.01):
        chunk_size = int(chunk_size)
        if chunk_size <= 0:
            raise TerrainError("INVALID_CHUNK_SIZE", chunk_size)
        self.chunk_size = chunk_size
        self.split_distance = float(split_distance)
        self.height_epsilon = float(height_epsilon)

        self._heightmap = None
        self._width = 0
        self._height = 0
        self._version = 0
        self._hole_mask = None
        self._chunks = {}
        self._view = (0.0, 0.0)
        self._build_serial = 0

    # ------------------------------------------------------------------
    # Terrain data
    # ------------------------------------------------------------------
    def set_heightmap(self, heights, version=1):
        if not heights or not heights[0]:
            raise TerrainError("INVALID_HEIGHTMAP")
        width = len(heights[0])
        if width == 0:
            raise TerrainError("INVALID_HEIGHTMAP")

        data = []
        for row in heights:
            if len(row) != width:
                raise TerrainError("INVALID_HEIGHTMAP")
            data.append([float(value) for value in row])

        self._heightmap = data
        self._width = width
        self._height = len(data)
        self._version = int(version)

        # A mask belongs to a particular map layout. Same-sized maps keep the
        # mask; a differently sized map cannot use the old mask meaningfully.
        if self._hole_mask is not None:
            if (len(self._hole_mask) != self._height or
                    any(len(row) != self._width for row in self._hole_mask)):
                self._hole_mask = None
        return self

    def set_hole_mask(self, mask):
        if mask is None:
            self._hole_mask = None
            for state in self._chunks.values():
                state["hole_mask"] = None
                self._refresh_chunk_indices(state)
            return self

        if not mask or not mask[0]:
            raise TerrainError("INVALID_HOLE_MASK")
        width = len(mask[0])
        data = []
        for row in mask:
            if len(row) != width:
                raise TerrainError("INVALID_HOLE_MASK")
            data.append([bool(value) for value in row])

        self._hole_mask = data
        for state in self._chunks.values():
            # Keep the chunk snapshot coherent with the mask that produced its
            # geometry. Existing chunks are re-indexed, never filled in.
            state["hole_mask"] = copy.deepcopy(data)
            self._refresh_chunk_indices(state)
        return self

    # ------------------------------------------------------------------
    # Sampling helpers
    # ------------------------------------------------------------------
    def _require_heightmap(self):
        if self._heightmap is None:
            raise TerrainError("NO_HEIGHTMAP")
        return self._heightmap

    @staticmethod
    def _sample_height(heightmap, x, y):
        height = len(heightmap)
        width = len(heightmap[0])
        if width == 1:
            x0 = x1 = 0
            tx = 0.0
        else:
            fx = min(max(float(x), 0.0), float(width - 1))
            x0 = int(math.floor(fx))
            x1 = min(x0 + 1, width - 1)
            tx = fx - x0

        if height == 1:
            y0 = y1 = 0
            ty = 0.0
        else:
            fy = min(max(float(y), 0.0), float(height - 1))
            y0 = int(math.floor(fy))
            y1 = min(y0 + 1, height - 1)
            ty = fy - y0

        h00 = heightmap[y0][x0]
        h10 = heightmap[y0][x1]
        h01 = heightmap[y1][x0]
        h11 = heightmap[y1][x1]
        top = h00 + (h10 - h00) * tx
        bottom = h01 + (h11 - h01) * tx
        return float(top + (bottom - top) * ty)

    @staticmethod
    def _normal_from_heightmap(heightmap, x, y):
        height = len(heightmap)
        width = len(heightmap[0])
        fx = min(max(float(x), 0.0), float(width - 1))
        fy = min(max(float(y), 0.0), float(height - 1))

        xa = max(0, int(math.floor(fx)) - 1)
        xb = min(width - 1, int(math.floor(fx)) + 1)
        ya = max(0, int(math.floor(fy)) - 1)
        yb = min(height - 1, int(math.floor(fy)) + 1)

        if xb == xa:
            dhdx = 0.0
        else:
            dhdx = ((TerrainLodStitcher._sample_height(heightmap, xb, fy) -
                     TerrainLodStitcher._sample_height(heightmap, xa, fy)) /
                    float(xb - xa))
        if yb == ya:
            dhdy = 0.0
        else:
            dhdy = ((TerrainLodStitcher._sample_height(heightmap, fx, yb) -
                     TerrainLodStitcher._sample_height(heightmap, fx, ya)) /
                    float(yb - ya))

        nx, ny, nz = -dhdx, 1.0, -dhdy
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0.0:
            return (0.0, 1.0, 0.0)
        return (nx / length, ny / length, nz / length)

    @staticmethod
    def _clamp01(value):
        return min(1.0, max(0.0, float(value)))

    def texture_weights(self, x, y):
        """Return deterministic, globally continuous texture blend weights."""
        heightmap = self._require_heightmap()
        normal = self._normal_from_heightmap(heightmap, x, y)
        slope = self._clamp01((1.0 - normal[1]) * 4.0)
        phase = (math.sin(float(x) * 0.37 + float(y) * 0.11) *
                 math.cos(float(y) * 0.29 - float(x) * 0.07))
        mud = self._clamp01(0.28 + 0.22 * phase + 0.25 * slope)
        rock = self._clamp01(0.08 + 0.72 * slope)
        grass = max(0.0, 1.0 - mud - rock)
        total = grass + mud + rock
        if total <= 0.0:
            return {"grass": 1.0, "mud": 0.0, "rock": 0.0, "sand": 0.0}
        grass /= total
        mud /= total
        rock /= total
        sand = 0.0
        return {"grass": grass, "mud": mud, "rock": rock, "sand": sand}

    def _texture_weight_list(self, weights):
        return [weights["grass"], weights["mud"],
                weights["rock"], weights["sand"]]

    # ------------------------------------------------------------------
    # Chunk ids and adjacency
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_chunk_id(chunk_id):
        if isinstance(chunk_id, (tuple, list)) and len(chunk_id) == 2:
            return int(chunk_id[0]), int(chunk_id[1])
        if isinstance(chunk_id, str):
            parts = chunk_id.split(":")
            if len(parts) == 2:
                try:
                    return int(parts[0]), int(parts[1])
                except ValueError:
                    pass
        raise TerrainError("INVALID_CHUNK_ID", chunk_id)

    @staticmethod
    def _make_chunk_id(chunk_x, chunk_y):
        return "{}:{}".format(int(chunk_x), int(chunk_y))

    def _state(self, chunk_id):
        key = self._make_chunk_id(*self._parse_chunk_id(chunk_id))
        state = self._chunks.get(key)
        if state is None:
            raise TerrainError("UNKNOWN_CHUNK", chunk_id)
        return state

    # ------------------------------------------------------------------
    # Mesh construction
    # ------------------------------------------------------------------
    @staticmethod
    def _outer_loop(chunk_size):
        points = []
        for x in range(chunk_size):
            points.append((x, 0))
        for y in range(chunk_size):
            points.append((chunk_size, y))
        for x in range(chunk_size, 0, -1):
            points.append((x, chunk_size))
        for y in range(chunk_size, 0, -1):
            points.append((0, y))
        return points

    @staticmethod
    def _area2(a, b, c):
        return ((b[0] - a[0]) * (c[1] - a[1]) -
                (b[1] - a[1]) * (c[0] - a[0]))

    @staticmethod
    def _fan_triangles(center, path):
        return [(center, path[index], path[index + 1])
                for index in range(len(path) - 1)]

    def _mesh_layout(self, lod):
        chunk_size = self.chunk_size
        step = 1 << min(int(lod), 30)
        if step <= 1:
            points = set()
            triangles = []
            for y in range(chunk_size):
                for x in range(chunk_size):
                    p00 = (x, y)
                    p10 = (x + 1, y)
                    p01 = (x, y + 1)
                    p11 = (x + 1, y + 1)
                    points.update((p00, p10, p01, p11))
                    triangles.append((p00, p10, p01))
                    triangles.append((p10, p11, p01))
            return points, triangles

        outer = self._outer_loop(chunk_size)
        points = set(outer)
        triangles = []

        xs = list(range(step, chunk_size, step))
        ys = list(range(step, chunk_size, step))
        if not xs or not ys:
            xs = [chunk_size / 2.0]
            ys = [chunk_size / 2.0]

        for y in ys:
            for x in xs:
                points.add((x, y))

        if len(xs) < 2 or len(ys) < 2:
            center = (xs[0] if len(xs) == 1 else chunk_size / 2.0,
                      ys[0] if len(ys) == 1 else chunk_size / 2.0)
            points.add(center)
            for i in range(len(outer)):
                triangles.append((center, outer[i],
                                  outer[(i + 1) % len(outer)]))
            return points, triangles

        # Coarse interior.
        for j in range(len(ys) - 1):
            for i in range(len(xs) - 1):
                p00 = (xs[i], ys[j])
                p10 = (xs[i + 1], ys[j])
                p01 = (xs[i], ys[j + 1])
                p11 = (xs[i + 1], ys[j + 1])
                triangles.append((p00, p10, p01))
                triangles.append((p10, p11, p01))

        # Water-tight transition between the fine outer border and the coarse
        # inner rectangle.  The square is partitioned into four side bands,
        # four corner squares and the inner rectangle; each region is fanned
        # independently so the pieces cannot overlap or leave a gap.
        left, right = xs[0], xs[-1]
        bottom, top = ys[0], ys[-1]

        bottom_path = [(x, 0) for x in range(left, right + 1)]
        triangles.extend(self._fan_triangles(
            (left, bottom), bottom_path + [(right, bottom)]))
        top_path = [(x, chunk_size) for x in range(left, right + 1)]
        triangles.extend(self._fan_triangles(
            (left, top), top_path + [(right, top)]))
        left_path = [(0, y) for y in range(bottom, top + 1)]
        triangles.extend(self._fan_triangles(
            (left, bottom), left_path + [(left, top)]))
        right_path = [(chunk_size, y) for y in range(bottom, top + 1)]
        triangles.extend(self._fan_triangles(
            (right, bottom), right_path + [(right, top)]))

        bl_path = ([(x, 0) for x in range(left, -1, -1)] +
                   [(0, y) for y in range(1, bottom + 1)])
        triangles.extend(self._fan_triangles((left, bottom), bl_path))
        br_path = ([(x, 0) for x in range(right, chunk_size + 1)] +
                   [(chunk_size, y) for y in range(1, bottom + 1)])
        triangles.extend(self._fan_triangles((right, bottom), br_path))
        tl_path = ([(x, chunk_size) for x in range(left, -1, -1)] +
                   [(0, y) for y in range(chunk_size - 1, top - 1, -1)])
        triangles.extend(self._fan_triangles((left, top), tl_path))
        tr_path = ([(x, chunk_size) for x in range(right, chunk_size + 1)] +
                   [(chunk_size, y)
                    for y in range(chunk_size - 1, top - 1, -1)])
        triangles.extend(self._fan_triangles((right, top), tr_path))
        return points, triangles

    @staticmethod
    def _triangle_bbox(triangle):
        xs = [point[0] for point in triangle]
        ys = [point[1] for point in triangle]
        return min(xs), min(ys), max(xs), max(ys)

    @classmethod
    def _triangle_hits_hole(cls, triangle, hole_mask):
        if hole_mask is None:
            return False
        min_x, min_y, max_x, max_y = cls._triangle_bbox(triangle)
        x_start = int(math.floor(min_x))
        x_end = int(math.ceil(max_x)) - 1
        y_start = int(math.floor(min_y))
        y_end = int(math.ceil(max_y)) - 1
        for y in range(max(0, y_start), min(len(hole_mask), y_end + 1)):
            row = hole_mask[y]
            for x in range(max(0, x_start), min(len(row), x_end + 1)):
                if row[x]:
                    return True
        return False

    def _clean_coordinate_triangles(self, triangles):
        filtered = []
        for triangle in triangles:
            area = self._area2(*triangle)
            if area == 0.0:
                continue
            if area < 0.0:
                triangle = (triangle[0], triangle[2], triangle[1])
            filtered.append(triangle)
        return filtered

    def _filter_triangle_indices(self, state, hole_mask):
        vertices = state["vertices"]
        result = []
        for tri in state.get("all_triangles", state.get("triangles", [])):
            coords = tuple((vertices[index]["x"], vertices[index]["y"])
                           for index in tri)
            if self._triangle_hits_hole(coords, hole_mask):
                continue
            result.append(tuple(tri))
        return result

    def _refresh_chunk_indices(self, state):
        hole_mask = state.get("hole_mask")
        for vertex in state["vertices"]:
            vertex["hole"] = self._is_hole_in_mask(
                hole_mask, vertex["x"], vertex["y"])
        triangles = self._filter_triangle_indices(state, hole_mask)
        state["triangles"] = triangles
        state["indices"] = [index for tri in triangles for index in tri]
        state["index_buffer"] = state["indices"]
        state["index_count"] = len(state["indices"])

    def _build_skirt(self, border_keys, border_heights, border_vertices,
                     chunk_id):
        border_values = list(border_heights.values())
        height_range = max(border_values) - min(border_values)
        depth = max(0.1, self.chunk_size * 0.1, height_range + 0.1)
        skirt = []
        skirt_indices = []
        for key in border_keys:
            x, y = key
            top_height = border_heights[key]
            skirt.append({
                "x": x,
                "y": y,
                "height": top_height - depth,
                "top_height": top_height,
                "chunk_id": chunk_id,
                "kind": "skirt",
            })
        for i in range(len(border_keys)):
            top_a = border_vertices[border_keys[i]]["index"]
            top_b = border_vertices[border_keys[(i + 1) % len(border_keys)]]["index"]
            skirt_indices.extend((top_a, len(skirt) + i,
                                  len(skirt) + (i + 1) % len(skirt)))
            skirt_indices.extend((top_b, len(skirt) + (i + 1) % len(skirt),
                                  len(skirt) + i))
        return skirt, skirt_indices

    def build_chunk(self, chunk_x, chunk_y, lod=0):
        heightmap = self._require_heightmap()
        chunk_x = int(chunk_x)
        chunk_y = int(chunk_y)
        lod = int(lod)
        if lod < 0:
            raise TerrainError("INVALID_LOD", lod)

        chunk_id = self._make_chunk_id(chunk_x, chunk_y)
        chunk_size = self.chunk_size
        origin_x = chunk_x * chunk_size
        origin_y = chunk_y * chunk_size
        version = self._version
        hole_mask = (copy.deepcopy(self._hole_mask)
                     if self._hole_mask is not None else None)

        points, coordinate_triangles = self._mesh_layout(lod)
        coordinate_triangles = self._clean_coordinate_triangles(
            coordinate_triangles)

        ordered_points = sorted(
            points, key=lambda point: (round(point[1], 9), round(point[0], 9)))
        vertices = []
        point_to_index = {}
        for local_x, local_y in ordered_points:
            global_x = origin_x + local_x
            global_y = origin_y + local_y
            point_index = len(vertices)
            point_to_index[(local_x, local_y)] = point_index
            normal = self._normal_from_heightmap(heightmap, global_x, global_y)
            weights = self.texture_weights(global_x, global_y)
            vertices.append({
                "x": global_x,
                "y": global_y,
                "height": self._sample_height(heightmap, global_x, global_y),
                "normal": normal,
                "texture_weights": weights,
                "weights": self._texture_weight_list(weights),
                "hole": self._is_hole_in_mask(hole_mask, global_x, global_y),
            })

        all_triangles = []
        for triangle in coordinate_triangles:
            all_triangles.append(tuple(point_to_index[point]
                                       for point in triangle))

        border_keys = []
        border_heights = {}
        border_vertices = {}
        for local_x, local_y in self._outer_loop(chunk_size):
            global_x = origin_x + local_x
            global_y = origin_y + local_y
            key = (global_x, global_y)
            border_keys.append(key)
            border_heights[key] = self._sample_height(
                heightmap, global_x, global_y)
            weights = self.texture_weights(global_x, global_y)
            border_vertices[key] = {
                "height": border_heights[key],
                "normal": self._normal_from_heightmap(
                    heightmap, global_x, global_y),
                "texture_weights": weights,
                "weights": self._texture_weight_list(weights),
                "index": point_to_index[(local_x, local_y)],
            }

        state = {
            "chunk_id": chunk_id,
            "chunk_x": chunk_x,
            "chunk_y": chunk_y,
            "lod": lod,
            "version": version,
            "heightmap_version": version,
            "heightmap": heightmap,
            "hole_mask": hole_mask,
            "origin": (origin_x, origin_y),
            "size": chunk_size,
            "bounds": (origin_x, origin_y,
                       origin_x + chunk_size, origin_y + chunk_size),
            "vertices": vertices,
            "vertex_buffer": vertices,
            "points": ordered_points,
            "all_triangles": all_triangles,
            "triangles": [],
            "indices": [],
            "index_buffer": [],
            "index_count": 0,
            "index_type": self.index_type(len(vertices)),
            "index_offset": 0,
            "vertex_offset": 0,
            "border_keys": border_keys,
            "border_heights": border_heights,
            "border_vertices": border_vertices,
            "morph_factor": 0.0,
            "state": "ready",
            "build_serial": 0,
        }
        self._refresh_chunk_indices(state)
        skirt, skirt_indices = self._build_skirt(
            border_keys, border_heights, border_vertices, chunk_id)
        state["skirt_vertices"] = skirt
        state["skirt_indices"] = skirt_indices
        state["skirt"] = state["skirt_vertices"]

        self._build_serial += 1
        state["build_serial"] = self._build_serial
        # Publishing is a single assignment. A LOD change therefore exposes
        # either the complete old buffer or the complete new buffer.
        self._chunks[chunk_id] = state
        return copy.deepcopy(state)

    # ------------------------------------------------------------------
    # Stitching and queries
    # ------------------------------------------------------------------
    def stitch(self, chunk_a, chunk_b):
        state_a = self._state(chunk_a)
        state_b = self._state(chunk_b)

        if (state_a["version"] != state_b["version"] or
                state_a["version"] != self._version):
            raise TerrainError(
                "STALE_CHUNK",
                {"a": state_a["version"], "b": state_b["version"],
                 "current": self._version})

        shared_keys = sorted(
            set(state_a["border_heights"]) &
            set(state_b["border_heights"]))
        if not shared_keys:
            return {
                "chunk_a": state_a["chunk_id"],
                "chunk_b": state_b["chunk_id"],
                "adjacent": False,
                "crack": False,
                "max_height_error": 0.0,
                "height_error": 0.0,
                "normal_match": True,
                "texture_weights_match": True,
                "stitch_indices": [],
                "indices": [],
                "version": state_a["version"],
            }

        max_height_error = 0.0
        normal_match = True
        weights_match = True
        stitch_indices = []
        for key in shared_keys:
            border_a = state_a["border_vertices"][key]
            border_b = state_b["border_vertices"][key]
            max_height_error = max(
                max_height_error,
                abs(state_a["border_heights"][key] -
                    state_b["border_heights"][key]))
            if any(abs(border_a["normal"][axis] -
                       border_b["normal"][axis]) > 1e-9
                   for axis in range(3)):
                normal_match = False
            if any(abs(border_a["texture_weights"][name] -
                       border_b["texture_weights"][name]) > 1e-9
                   for name in border_a["texture_weights"]):
                weights_match = False
            stitch_indices.append([border_a["index"], border_b["index"]])

        crack = max_height_error > self.height_epsilon + 1e-12
        return {
            "chunk_a": state_a["chunk_id"],
            "chunk_b": state_b["chunk_id"],
            "adjacent": True,
            "crack": crack,
            "max_height_error": max_height_error,
            "height_error": max_height_error,
            "normal_match": normal_match,
            "texture_weights_match": weights_match,
            "stitch_indices": stitch_indices,
            "indices": stitch_indices,
            "version": state_a["version"],
        }

    def border_heights(self, chunk_id):
        state = self._state(chunk_id)
        return dict(state["border_heights"])

    def render_height(self, x, y):
        return self._sample_height(self._require_heightmap(), x, y)

    def physics_height(self, x, y):
        # Physics intentionally uses the same canonical sampler and snapshot
        # as rendering, rather than whichever LOD chunk happens to be nearby.
        return self._sample_height(self._require_heightmap(), x, y)

    def normal_at(self, x, y):
        return self._normal_from_heightmap(
            self._require_heightmap(), x, y)

    def set_view(self, x, z):
        self._view = (float(x), float(z))
        return self

    def morph_factor(self, distance):
        if self.split_distance <= 0.0:
            return 1.0 if float(distance) > 0.0 else 0.0
        return self._clamp01(float(distance) / self.split_distance)

    def apply_rebuild(self, chunk_id, result_version, result=None):
        state = self._state(chunk_id)
        result_version = int(result_version)

        if result_version < state["version"]:
            return {
                "chunk_id": state["chunk_id"],
                "applied": False,
                "stale": True,
                "version": state["version"],
                "lod": state["lod"],
            }

        if result_version > state["version"]:
            return {
                "chunk_id": state["chunk_id"],
                "applied": False,
                "stale": False,
                "version": state["version"],
                "lod": state["lod"],
                "reason": "VERSION_AHEAD",
            }

        if result is not None:
            if int(result.get("version", result_version)) != result_version:
                return {
                    "chunk_id": state["chunk_id"],
                    "applied": False,
                    "stale": True,
                    "version": state["version"],
                    "lod": state["lod"],
                }
            replacement = copy.deepcopy(result)
            replacement["chunk_id"] = state["chunk_id"]
            replacement["version"] = result_version
            replacement["heightmap_version"] = result_version
            self._build_serial += 1
            replacement["build_serial"] = self._build_serial
            self._chunks[state["chunk_id"]] = replacement
            state = replacement
        else:
            state["state"] = "rebuilt"

        return {
            "chunk_id": state["chunk_id"],
            "applied": True,
            "stale": False,
            "version": state["version"],
            "lod": state["lod"],
        }

    def chunk_state(self, chunk_id):
        return copy.deepcopy(self._state(chunk_id))

    def visible_chunks(self, camera_chunk):
        chunk_x, chunk_y = self._parse_chunk_id(camera_chunk)
        visible = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                visible.append(self._make_chunk_id(chunk_x + dx, chunk_y + dy))
        return visible

    @staticmethod
    def _is_hole_in_mask(mask, x, y):
        if mask is None:
            return False
        ix = int(math.floor(float(x)))
        iy = int(math.floor(float(y)))
        if iy < 0 or iy >= len(mask):
            return False
        if ix < 0 or ix >= len(mask[iy]):
            return False
        return bool(mask[iy][ix])

    def is_hole(self, x, y):
        return self._is_hole_in_mask(self._hole_mask, x, y)

    def index_type(self, vertex_count):
        count = int(vertex_count)
        if count <= self._UINT16_MAX_VERTICES:
            return "uint16"
        return "uint32"

    def skirt_vertices(self, chunk_id):
        state = self._state(chunk_id)
        return copy.deepcopy(state["skirt_vertices"])

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def dump(self):
        chunks = {}
        for chunk_id, state in self._chunks.items():
            chunks[chunk_id] = {
                "chunk_id": state["chunk_id"],
                "chunk_x": state["chunk_x"],
                "chunk_y": state["chunk_y"],
                "lod": state["lod"],
                "version": state["version"],
                "heightmap_version": state["heightmap_version"],
                "heightmap": [list(row) for row in state["heightmap"]],
                "hole_mask": (None if state["hole_mask"] is None else
                              [list(row) for row in state["hole_mask"]]),
                "origin": list(state["origin"]),
                "size": state["size"],
                "bounds": list(state["bounds"]),
                "vertices": [self._dump_vertex(vertex)
                             for vertex in state["vertices"]],
                "points": [list(point) for point in state["points"]],
                "all_triangles": [list(tri)
                                  for tri in state["all_triangles"]],
                "triangles": [list(tri) for tri in state["triangles"]],
                "indices": list(state["indices"]),
                "index_buffer": list(state["indices"]),
                "index_count": state["index_count"],
                "index_type": state["index_type"],
                "index_offset": state["index_offset"],
                "vertex_offset": state["vertex_offset"],
                "border_keys": [list(key) for key in state["border_keys"]],
                "border_heights": [[key[0], key[1], value]
                                   for key, value in
                                   state["border_heights"].items()],
                "border_vertices": [
                    [key[0], key[1], self._dump_border(border)]
                    for key, border in state["border_vertices"].items()],
                "skirt_vertices": copy.deepcopy(state["skirt_vertices"]),
                "skirt": copy.deepcopy(state["skirt_vertices"]),
                "skirt_indices": list(state["skirt_indices"]),
                "morph_factor": state["morph_factor"],
                "state": state["state"],
                "build_serial": state["build_serial"],
            }
        return {
            "format": "terrain_lod_stitcher.v1",
            "chunk_size": self.chunk_size,
            "split_distance": self.split_distance,
            "height_epsilon": self.height_epsilon,
            "heightmap": (None if self._heightmap is None else
                          [list(row) for row in self._heightmap]),
            "width": self._width,
            "height": self._height,
            "version": self._version,
            "hole_mask": (None if self._hole_mask is None else
                          [list(row) for row in self._hole_mask]),
            "view": [self._view[0], self._view[1]],
            "build_serial": self._build_serial,
            "chunks": chunks,
        }

    @staticmethod
    def _dump_vertex(vertex):
        return {
            "x": vertex["x"],
            "y": vertex["y"],
            "height": vertex["height"],
            "normal": list(vertex["normal"]),
            "texture_weights": dict(vertex["texture_weights"]),
            "weights": list(vertex["weights"]),
            "hole": bool(vertex["hole"]),
        }

    @staticmethod
    def _dump_border(border):
        return {
            "height": border["height"],
            "normal": list(border["normal"]),
            "texture_weights": dict(border["texture_weights"]),
            "weights": list(border["weights"]),
            "index": border["index"],
        }

    @staticmethod
    def _load_vertex(vertex):
        result = dict(vertex)
        result["normal"] = tuple(vertex["normal"])
        result["texture_weights"] = dict(vertex["texture_weights"])
        result["weights"] = list(vertex["weights"])
        result["hole"] = bool(vertex.get("hole", False))
        return result

    @staticmethod
    def _load_border(border):
        result = dict(border)
        result["normal"] = tuple(border["normal"])
        result["texture_weights"] = dict(border["texture_weights"])
        result["weights"] = list(border["weights"])
        return result

    def load(self, state):
        self.chunk_size = int(state.get("chunk_size", self.chunk_size))
        self.split_distance = float(
            state.get("split_distance", self.split_distance))
        self.height_epsilon = float(
            state.get("height_epsilon", self.height_epsilon))
        self._heightmap = (None if state.get("heightmap") is None else
                           [[float(value) for value in row]
                            for row in state["heightmap"]])
        self._width = int(state.get("width", 0))
        self._height = int(state.get("height", 0))
        self._version = int(state.get("version", 0))
        self._hole_mask = (None if state.get("hole_mask") is None else
                           [[bool(value) for value in row]
                            for row in state["hole_mask"]])
        view = state.get("view", [0.0, 0.0])
        self._view = (float(view[0]), float(view[1]))
        self._build_serial = int(state.get("build_serial", 0))
        self._chunks = {}

        for chunk_id, dumped in state.get("chunks", {}).items():
            chunk = {
                "chunk_id": dumped["chunk_id"],
                "chunk_x": int(dumped["chunk_x"]),
                "chunk_y": int(dumped["chunk_y"]),
                "lod": int(dumped["lod"]),
                "version": int(dumped["version"]),
                "heightmap_version": int(dumped["heightmap_version"]),
                "heightmap": [[float(value) for value in row]
                              for row in dumped["heightmap"]],
                "hole_mask": (None if dumped.get("hole_mask") is None else
                              [[bool(value) for value in row]
                               for row in dumped["hole_mask"]]),
                "origin": tuple(dumped["origin"]),
                "size": int(dumped["size"]),
                "bounds": tuple(dumped["bounds"]),
                "vertices": [self._load_vertex(vertex)
                             for vertex in dumped["vertices"]],
                "points": [tuple(point) for point in dumped["points"]],
                "all_triangles": [tuple(tri)
                                  for tri in dumped["all_triangles"]],
                "triangles": [tuple(tri) for tri in dumped["triangles"]],
                "indices": list(dumped["indices"]),
                "index_count": int(dumped["index_count"]),
                "index_type": dumped["index_type"],
                "index_offset": int(dumped["index_offset"]),
                "vertex_offset": int(dumped["vertex_offset"]),
                "border_keys": [tuple(key) for key in dumped["border_keys"]],
                "border_heights": {
                    (key[0], key[1]): float(key[2])
                    for key in dumped["border_heights"]},
                "border_vertices": {
                    (entry[0], entry[1]): self._load_border(entry[2])
                    for entry in dumped["border_vertices"]},
                "skirt_vertices": copy.deepcopy(dumped["skirt_vertices"]),
                "skirt_indices": list(dumped["skirt_indices"]),
                "morph_factor": float(dumped["morph_factor"]),
                "state": dumped["state"],
                "build_serial": int(dumped["build_serial"]),
            }
            chunk["vertex_buffer"] = chunk["vertices"]
            chunk["index_buffer"] = chunk["indices"]
            chunk["skirt"] = chunk["skirt_vertices"]
            self._chunks[chunk_id] = chunk
        return self
