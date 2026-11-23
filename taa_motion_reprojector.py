"""Temporal anti-aliasing history and reprojection bookkeeping.

The implementation deliberately keeps the temporal state separate for every
view.  A display frame is described by an immutable camera snapshot; motion
vectors may only be produced from (and committed with) that snapshot.
"""

from __future__ import annotations

import copy
import json
import math


class TaaError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_FORMAT_VERSION = 1
_EPSILON = 1e-9

_NON_ACCUMULATING_LAYERS = frozenset(
    {
        "particle",
        "particles",
        "vfx",
        "transparent",
        "translucent",
        "alpha",
        "water",
        "ui",
        "hud",
        "text",
        "overlay",
    }
)


def _pair(value, name):
    try:
        x, y = value
    except (TypeError, ValueError):
        raise TaaError("INVALID_ARGUMENT", "%s must contain two values" % name)
    if isinstance(x, bool) or isinstance(y, bool):
        raise TaaError("INVALID_ARGUMENT", "%s must be numeric" % name)
    try:
        return (float(x), float(y))
    except (TypeError, ValueError):
        raise TaaError("INVALID_ARGUMENT", "%s must be numeric" % name)


def _resolution(value):
    w, h = _pair(value, "resolution")
    if w <= 0.0 or h <= 0.0:
        raise TaaError("INVALID_RESOLUTION", "resolution must be positive")
    return (int(w) if w.is_integer() else w, int(h) if h.is_integer() else h)


def _deep(value):
    return copy.deepcopy(value)


def _close(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=_EPSILON)
    return a == b


def _matrix_equal(left, right):
    if left is None or right is None:
        return left is right
    if len(left) != len(right):
        return False
    for row_a, row_b in zip(left, right):
        try:
            if len(row_a) != len(row_b):
                return False
        except TypeError:
            if not _close(row_a, row_b):
                return False
            continue
        if not all(_close(a, b) for a, b in zip(row_a, row_b)):
            return False
    return True


def _clamp(value, limit):
    return max(-limit, min(limit, value))


def _layer_name(layer):
    if layer is None:
        return ""
    name = str(layer).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "particle_system": "particle",
        "transparent_water": "water",
        "water_surface": "water",
        "user_interface": "ui",
        "gui": "ui",
    }
    return aliases.get(name, name)


class TaaMotionReprojector:
    """Temporal anti-aliasing history and screen-space motion vectors."""

    def __init__(self, blend=0.1, max_history=0.9):
        try:
            blend = float(blend)
            max_history = float(max_history)
        except (TypeError, ValueError):
            raise TaaError("INVALID_BLEND", "blend values must be numeric")
        if not 0.0 <= blend <= 1.0 or not 0.0 <= max_history <= 1.0:
            raise TaaError("INVALID_BLEND", "blend values must be in [0, 1]")

        self.blend = blend
        self.max_history = max_history
        self._views = {}

    # ------------------------------------------------------------------
    # View and frame lifecycle
    # ------------------------------------------------------------------

    def _new_view(self, view_id):
        return {
            "view_id": view_id,
            "resolution": (64, 64),
            "camera_id": None,
            "projection": None,
            "snapshot": None,
            "previous_jitter": (0.0, 0.0),
            "frame_version": 0,
            "frame_count": 0,
            "blend": self.blend,
            "history_valid": True,
            "history_version": None,
            "history_reason": None,
            "history_camera": None,
            "history_projection": None,
            "history_resolution": None,
            "object_states": {},
            "motions": {},
        }

    def _view(self, view_id="main"):
        view_id = str(view_id)
        if view_id not in self._views:
            self._views[view_id] = self._new_view(view_id)
        return self._views[view_id]

    def create_view(self, view_id):
        """Create an independent history view.

        Creating an existing view is intentionally idempotent: split-screen
        setup code commonly registers the same view on every frame.
        """

        view = self._view(view_id)
        return {"view_id": view["view_id"], "history_valid": view["history_valid"]}

    def begin_frame(
        self,
        camera_id,
        projection,
        jitter=(0.0, 0.0),
        resolution=(64, 64),
        view_id="main",
    ):
        view = self._view(view_id)
        resolution = _resolution(resolution)
        jitter = _pair(jitter, "jitter")
        projection = _deep(projection)
        if projection is None:
            raise TaaError("INVALID_PROJECTION", "projection is required")

        old_snapshot = view["snapshot"]
        invalidate_reason = None
        if old_snapshot is not None:
            if camera_id != old_snapshot["camera_id"]:
                invalidate_reason = "camera_cut"
            elif not _matrix_equal(projection, old_snapshot["projection"]):
                invalidate_reason = "projection_mismatch"
            elif resolution != old_snapshot["resolution"]:
                invalidate_reason = "resolution_change"

        previous_jitter = (
            old_snapshot["jitter"] if old_snapshot is not None else (0.0, 0.0)
        )
        if invalidate_reason is not None:
            self.invalidate_history(invalidate_reason, view_id=view["view_id"])

        if view["frame_count"] == 0:
            view["blend"] = min(self.blend, self.max_history)
        else:
            view["blend"] = min(
                self.max_history,
                view["blend"] + (self.max_history - view["blend"]) * self.blend,
            )

        version = view["frame_version"] + 1
        snapshot_id = "%s:%d" % (view["view_id"], version)
        effective_projection = self._jitter_projection(
            projection, jitter, resolution
        )
        view.update(
            {
                "resolution": resolution,
                "camera_id": camera_id,
                "projection": projection,
                "previous_jitter": previous_jitter,
                "frame_version": version,
                "frame_count": view["frame_count"] + 1,
                "snapshot": {
                    "id": snapshot_id,
                    "version": version,
                    "camera_id": camera_id,
                    "projection": _deep(projection),
                    "jittered_projection": _deep(effective_projection),
                    "jitter": jitter,
                    "resolution": resolution,
                    "stale": False,
                },
            }
        )
        view["motions"] = {}
        return self.frame_state(view_id=view["view_id"])

    def set_resolution(self, resolution, view_id="main"):
        view = self._view(view_id)
        resolution = _resolution(resolution)
        if resolution == view["resolution"]:
            return

        self.invalidate_history("resolution_change", view_id=view["view_id"])
        view["resolution"] = resolution
        if view["snapshot"] is not None:
            view["snapshot"]["resolution"] = resolution
            view["snapshot"]["stale"] = True
            view["snapshot"]["jittered_projection"] = self._jitter_projection(
                view["snapshot"]["projection"],
                view["snapshot"]["jitter"],
                resolution,
            )

    def invalidate_history(self, reason, view_id="main"):
        """Discard the complete history for one view."""

        view = self._view(view_id)
        view["history_valid"] = False
        view["history_reason"] = reason
        view["history_version"] = None
        view["history_camera"] = None
        view["history_projection"] = None
        view["history_resolution"] = None
        view["object_states"].clear()
        view["motions"].clear()
        view["blend"] = min(self.blend, self.max_history)

    def history_valid(self, view_id="main"):
        return bool(self._view(view_id)["history_valid"])

    def commit(self, history_version, view_id="main"):
        """Commit the current frame snapshot as the next history frame.

        A version mismatch is fatal for the view's history.  The mismatched
        history is dropped before the error is raised, so a caller cannot
        accidentally blend with it on a subsequent frame.
        """

        view = self._view(view_id)
        view_id = view["view_id"]
        snapshot = view["snapshot"]
        if snapshot is None or history_version != view["frame_version"]:
            self.invalidate_history("frame_mismatch", view_id=view_id)
            raise TaaError(
                "FRAME_MISMATCH",
                "commit version %r does not match frame %r"
                % (history_version, view["frame_version"]),
            )
        if snapshot["stale"]:
            self.invalidate_history("camera_snapshot_mismatch", view_id=view_id)
            raise TaaError(
                "CAMERA_SNAPSHOT_MISMATCH",
                "the frame snapshot was invalidated before commit",
            )

        snapshot_id = snapshot["id"]
        bad_motion = any(
            motion.get("snapshot_id") != snapshot_id
            for motion in view["motions"].values()
        )
        if bad_motion:
            self.invalidate_history("camera_snapshot_mismatch", view_id=view_id)
            raise TaaError(
                "CAMERA_SNAPSHOT_MISMATCH",
                "motion vectors were produced from a different frame snapshot",
            )

        if view["history_valid"] and view["history_projection"] is not None:
            same_projection = _matrix_equal(
                snapshot["projection"], view["history_projection"]
            )
            same_resolution = snapshot["resolution"] == view["history_resolution"]
            same_camera = snapshot["camera_id"] == view["history_camera"]
            if not (same_projection and same_resolution and same_camera):
                self.invalidate_history(
                    "history_snapshot_mismatch", view_id=view_id
                )
                raise TaaError(
                    "CAMERA_SNAPSHOT_MISMATCH",
                    "history snapshot does not match the current frame",
                )

        view["history_version"] = history_version
        view["history_camera"] = snapshot["camera_id"]
        view["history_projection"] = _deep(snapshot["projection"])
        view["history_resolution"] = snapshot["resolution"]
        view["history_valid"] = True
        view["history_reason"] = None
        return self.frame_state(view_id=view_id)

    def frame_state(self, view_id="main"):
        view = self._view(view_id)
        snapshot = view["snapshot"]
        if snapshot is None:
            return {
                "view_id": view["view_id"],
                "camera_id": None,
                "projection": None,
                "base_projection": None,
                "effective_projection": None,
                "jittered_projection": None,
                "jitter": (0.0, 0.0),
                "resolution": view["resolution"],
                "frame_version": view["frame_version"],
                "snapshot_id": None,
                "blend": view["blend"],
                "history_valid": view["history_valid"],
                "history_version": view["history_version"],
                "history_reason": view["history_reason"],
                "object_count": len(view["object_states"]),
            }
        return {
            "view_id": view["view_id"],
            "camera_id": snapshot["camera_id"],
            "projection": _deep(snapshot["jittered_projection"]),
            "base_projection": _deep(snapshot["projection"]),
            "effective_projection": _deep(snapshot["jittered_projection"]),
            "jittered_projection": _deep(snapshot["jittered_projection"]),
            "jitter": snapshot["jitter"],
            "resolution": snapshot["resolution"],
            "frame_version": snapshot["version"],
            "snapshot_id": snapshot["id"],
            "blend": view["blend"],
            "history_valid": view["history_valid"],
            "history_version": view["history_version"],
            "history_reason": view["history_reason"],
            "object_count": len(view["object_states"]),
            "stale": snapshot["stale"],
        }

    # ------------------------------------------------------------------
    # Motion vectors and reprojection
    # ------------------------------------------------------------------

    def object_motion(
        self,
        object_id,
        prev_ndc,
        curr_ndc,
        teleport=False,
        view_id="main",
        prev_transform=None,
        curr_transform=None,
        prev_bones=None,
        bones=None,
        skeleton=None,
        layer="opaque",
    ):
        """Return a motion vector in screen-space pixels.

        ``prev_ndc``/``curr_ndc`` are normalized device coordinates.  The
        returned vector also contains the change in the frame's jitter, which
        is required when sampling a previously jittered history buffer.
        """

        view = self._view(view_id)
        snapshot = view["snapshot"]
        if snapshot is None or snapshot["stale"]:
            raise TaaError(
                "CAMERA_SNAPSHOT_MISMATCH",
                "object motion requires the current frame camera snapshot",
            )

        object_id = str(object_id)
        state = view["object_states"].get(object_id)
        if prev_ndc is None:
            prev_ndc = state["curr_ndc"] if state is not None else curr_ndc
        if not teleport and state is not None:
            if prev_transform is None:
                prev_transform = state.get("curr_transform")
            if prev_bones is None:
                prev_bones = state.get("bones")
        if curr_ndc is None:
            if state is None:
                raise TaaError("INVALID_ARGUMENT", "curr_ndc is required")
            curr_ndc = state["curr_ndc"]
        prev_ndc = _pair(prev_ndc, "prev_ndc")
        curr_ndc = _pair(curr_ndc, "curr_ndc")

        if teleport:
            self.invalidate_history("object_teleport", view_id=view["view_id"])

        width, height = snapshot["resolution"]
        dx = (curr_ndc[0] - prev_ndc[0]) * width * 0.5
        dy = (curr_ndc[1] - prev_ndc[1]) * height * 0.5
        jitter_dx = snapshot["jitter"][0] - view["previous_jitter"][0]
        jitter_dy = snapshot["jitter"][1] - view["previous_jitter"][1]
        motion = (
            _clamp(dx + jitter_dx, width * 0.5),
            _clamp(dy + jitter_dy, height * 0.5),
        )
        if teleport:
            motion = (0.0, 0.0)

        current_bones = bones if bones is not None else skeleton
        if current_bones is None and not teleport and state is not None:
            current_bones = state.get("bones")
        bone_motion = self._bone_motion(
            prev_bones,
            current_bones,
            snapshot["resolution"],
            snapshot["jitter"],
            view["previous_jitter"],
        )
        record = {
            "object_id": object_id,
            "prev_ndc": prev_ndc,
            "curr_ndc": curr_ndc,
            "prev_transform": _deep(prev_transform),
            "curr_transform": _deep(curr_transform),
            "frame_version": snapshot["version"],
            "snapshot_id": snapshot["id"],
            "teleport": bool(teleport),
            "prev_bones": _deep(prev_bones),
            "bones": _deep(current_bones),
            "bone_motion": _deep(bone_motion),
        }
        view["object_states"][object_id] = record
        view["motions"][object_id] = {
            "snapshot_id": snapshot["id"],
            "motion": motion,
        }

        return {
            "object_id": object_id,
            "motion": motion,
            "motion_pixels": motion,
            "motion_normalized": (
                motion[0] / (width * 0.5),
                motion[1] / (height * 0.5),
            ),
            "prev_ndc": prev_ndc,
            "curr_ndc": curr_ndc,
            "teleport": bool(teleport),
            "frame_version": snapshot["version"],
            "snapshot_id": snapshot["id"],
            "view_id": view["view_id"],
            "resolution": snapshot["resolution"],
            "accumulate": self.should_accumulate(layer),
            "bone_motion": bone_motion,
        }

    def _bone_motion(
        self, prev_bones, curr_bones, resolution, jitter, previous_jitter
    ):
        if prev_bones is None or curr_bones is None:
            return {}
        if not isinstance(prev_bones, dict) or not isinstance(curr_bones, dict):
            raise TaaError("INVALID_ARGUMENT", "bones must be mappings")

        width, height = resolution
        jitter_dx = jitter[0] - previous_jitter[0]
        jitter_dy = jitter[1] - previous_jitter[1]
        result = {}
        for name in sorted(set(prev_bones).intersection(curr_bones)):
            prev_point = self._bone_point(prev_bones[name])
            curr_point = self._bone_point(curr_bones[name])
            if prev_point is None or curr_point is None:
                continue
            result[name] = (
                _clamp(
                    (curr_point[0] - prev_point[0]) * width * 0.5 + jitter_dx,
                    width * 0.5,
                ),
                _clamp(
                    (curr_point[1] - prev_point[1]) * height * 0.5 + jitter_dy,
                    height * 0.5,
                ),
            )
        return result

    @staticmethod
    def _bone_point(value):
        if isinstance(value, dict):
            for key in ("ndc", "position_ndc", "point"):
                if key in value:
                    return _pair(value[key], "bone point")
            return None
        try:
            if len(value) == 2 and all(
                isinstance(component, (int, float)) for component in value
            ):
                return _pair(value, "bone point")
        except TypeError:
            pass
        # A bone matrix is still valid animation state, but a screen-space
        # point cannot be recovered without the object's projection.
        return None

    def reproject(
        self,
        history_index,
        motion,
        resolution=None,
        view_id="main",
        motion_space="auto",
    ):
        """Map a motion vector to a resolution-independent history UV."""

        view = self._view(view_id)
        if not view["history_valid"]:
            raise TaaError(
                "HISTORY_INVALIDATED",
                view["history_reason"] or "history is invalid",
            )

        if (
            view["history_version"] is not None
            and history_index != view["history_version"]
        ):
            self.invalidate_history("history_version_mismatch", view_id=view_id)
            raise TaaError(
                "HISTORY_VERSION_MISMATCH",
                "history index %r does not match committed version %r"
                % (history_index, view["history_version"]),
            )

        if resolution is None:
            resolution = view["resolution"]
        else:
            resolution = _resolution(resolution)
        normalized = self._normalized_motion(
            motion, resolution, view, motion_space
        )

        # The history sample is the current screen centre minus motion.
        uv = (0.5 - normalized[0] * 0.5, 0.5 - normalized[1] * 0.5)
        uv = (
            max(0.0, min(1.0, uv[0])),
            max(0.0, min(1.0, uv[1])),
        )
        return {
            "uv": uv,
            "history_index": history_index,
            "normalized_motion": normalized,
            "pixel_motion": (
                normalized[0] * resolution[0] * 0.5,
                normalized[1] * resolution[1] * 0.5,
            ),
            "resolution": resolution,
            "view_id": view["view_id"],
        }

    def _normalized_motion(self, motion, resolution, view, motion_space):
        if isinstance(motion, dict):
            if "normalized_motion" in motion:
                return _pair(motion["normalized_motion"], "motion")
            if "motion_normalized" in motion:
                return _pair(motion["motion_normalized"], "motion")
            if "motion" in motion:
                pixels = _pair(motion["motion"], "motion")
                source_res = _resolution(motion.get("resolution", resolution))
            else:
                raise TaaError("INVALID_ARGUMENT", "motion dictionary is empty")
        else:
            pixels = _pair(motion, "motion")
            source_res = resolution

        if motion_space == "normalized":
            return pixels
        if motion_space == "pixels":
            return self._pixels_to_normalized(pixels, source_res)
        if motion_space != "auto":
            raise TaaError("INVALID_ARGUMENT", "unknown motion space")

        if isinstance(motion, dict):
            return self._pixels_to_normalized(pixels, source_res)
        # A tuple supplied to reproject is normalized.  Large tuples are
        # accepted as pixels for compatibility with object_motion() output.
        if max(abs(pixels[0]), abs(pixels[1])) > 1.0:
            return self._pixels_to_normalized(pixels, source_res)
        return pixels

    @staticmethod
    def _pixels_to_normalized(pixels, resolution):
        width, height = resolution
        return (
            pixels[0] / (width * 0.5),
            pixels[1] / (height * 0.5),
        )

    # ------------------------------------------------------------------
    # Layer policy
    # ------------------------------------------------------------------

    def should_accumulate(self, layer):
        return _layer_name(layer) not in _NON_ACCUMULATING_LAYERS

    def layer_policy(self, layer):
        name = _layer_name(layer)
        if not self.should_accumulate(name):
            if name in {"ui", "hud", "text", "overlay"}:
                return {"layer": name, "accumulate": False, "motion": "exclude"}
            return {"layer": name, "accumulate": False, "motion": "clip"}
        return {"layer": name, "accumulate": True, "motion": "reproject"}

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def dump(self):
        return {
            "format_version": _FORMAT_VERSION,
            "blend": self.blend,
            "max_history": self.max_history,
            "views": _deep(self._views),
        }

    def load(self, state):
        if isinstance(state, (str, bytes, bytearray)):
            try:
                state = json.loads(state)
            except (TypeError, ValueError) as exc:
                raise TaaError("INVALID_STATE", str(exc))
        if not isinstance(state, dict):
            raise TaaError("INVALID_STATE", "state must be a mapping")
        if state.get("format_version") != _FORMAT_VERSION:
            raise TaaError(
                "HISTORY_VERSION_MISMATCH",
                "unsupported history format version",
            )
        views = state.get("views")
        if not isinstance(views, dict):
            raise TaaError("INVALID_STATE", "views must be a mapping")

        restored = {}
        for view_id, raw_view in views.items():
            if not isinstance(raw_view, dict):
                raise TaaError("INVALID_STATE", "invalid view state")
            try:
                view = self._new_view(str(view_id))
                view.update(_deep(raw_view))
                view["view_id"] = str(view_id)
                view["resolution"] = _resolution(view["resolution"])
                view["previous_jitter"] = _pair(
                    view["previous_jitter"], "previous_jitter"
                )
                if view["history_resolution"] is not None:
                    view["history_resolution"] = _resolution(
                        view["history_resolution"]
                    )
                snapshot = view.get("snapshot")
                if snapshot is not None:
                    snapshot["resolution"] = _resolution(snapshot["resolution"])
                    snapshot["jitter"] = _pair(snapshot["jitter"], "jitter")
                view["object_states"] = dict(view.get("object_states") or {})
                view["motions"] = dict(view.get("motions") or {})
            except (KeyError, TypeError, ValueError) as exc:
                raise TaaError("INVALID_STATE", str(exc))

            if view["history_valid"]:
                consistent = True
                if snapshot is not None and snapshot.get("stale"):
                    consistent = False
                if view["history_version"] is not None and (
                    view["history_projection"] is None
                ):
                    consistent = False
                if view["history_projection"] is not None:
                    consistent = consistent and (
                        view["history_resolution"] == view["resolution"]
                        and view["history_camera"] == view["camera_id"]
                        and (
                            snapshot is None
                            or _matrix_equal(
                                snapshot["projection"],
                                view["history_projection"],
                            )
                        )
                    )
                if not consistent:
                    view["history_valid"] = False
                    view["history_reason"] = "history_snapshot_mismatch"
                    view["history_version"] = None
            restored[str(view_id)] = view

        try:
            blend = float(state["blend"])
            max_history = float(state["max_history"])
        except (KeyError, TypeError, ValueError):
            raise TaaError("INVALID_STATE", "missing blend values")
        if not 0.0 <= blend <= 1.0 or not 0.0 <= max_history <= 1.0:
            raise TaaError("INVALID_STATE", "blend values out of range")

        self.blend = blend
        self.max_history = max_history
        self._views = restored
        return self

    @staticmethod
    def _jitter_projection(projection, jitter, resolution):
        """Fold a pixel jitter offset into a projective matrix.

        Translation lives in the third column for both 3x3 and 4x4
        projection matrices.  A 2x2 linear matrix cannot represent an
        offset, so it is preserved and the jitter remains available in the
        snapshot for motion-vector calculation.
        """

        if projection is None:
            return None
        try:
            rows = len(projection)
            columns = len(projection[0])
        except (TypeError, IndexError):
            return _deep(projection)
        if rows not in (3, 4) or columns not in (3, 4):
            return _deep(projection)
        try:
            matrix = [list(_deep(row)) for row in projection]
        except TypeError:
            return _deep(projection)
        width, height = resolution
        offset_x = 2.0 * jitter[0] / width
        offset_y = 2.0 * jitter[1] / height
        try:
            matrix[0][2] = float(matrix[0][2]) + offset_x
            matrix[1][2] = float(matrix[1][2]) + offset_y
        except (TypeError, ValueError, IndexError):
            return _deep(projection)
        return matrix
