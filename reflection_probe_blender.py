import math


class ProbeError(ValueError):
    """Raised when a reflection probe operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _vector(value, name, length=3):
    try:
        result = tuple(float(part) for part in value)
    except (TypeError, ValueError):
        raise ProbeError("invalid_argument", {"argument": name})
    if len(result) != length or not all(math.isfinite(part) for part in result):
        raise ProbeError("invalid_argument", {"argument": name})
    return result


def _mat_mul(left, right):
    return [
        [sum(left[row][k] * right[k][col] for k in range(3))
         for col in range(3)]
        for row in range(3)
    ]


def _rotation_matrix(rotation):
    """Return the probe's local-to-world rotation.

    Euler angles are in degrees and use the Z * Y * X order.  Yaw-only
    rotations keep the convention documented on ``ReflectionProbeBlender``.
    """

    rx, ry, rz = (math.radians(angle) for angle in rotation)
    cos_x, sin_x = math.cos(rx), math.sin(rx)
    cos_y, sin_y = math.cos(ry), math.sin(ry)
    cos_z, sin_z = math.cos(rz), math.sin(rz)

    rot_x = [
        [1.0, 0.0, 0.0],
        [0.0, cos_x, -sin_x],
        [0.0, sin_x, cos_x],
    ]
    rot_y = [
        [cos_y, 0.0, sin_y],
        [0.0, 1.0, 0.0],
        [-sin_y, 0.0, cos_y],
    ]
    rot_z = [
        [cos_z, -sin_z, 0.0],
        [sin_z, cos_z, 0.0],
        [0.0, 0.0, 1.0],
    ]
    return _mat_mul(rot_z, _mat_mul(rot_y, rot_x))


def _transpose(matrix):
    return [[matrix[col][row] for col in range(3)] for row in range(3)]


def _mat_vec(matrix, vector):
    return tuple(
        sum(matrix[row][col] * vector[col] for col in range(3))
        for row in range(3)
    )


def _world_to_local(point, probe):
    offset = tuple(point[i] - probe["position"][i] for i in range(3))
    return _mat_vec(probe["inverse_rotation"], offset)


def _box_contains(point, probe, tolerance=1e-9):
    local = _world_to_local(point, probe)
    return all(
        abs(local[axis]) <= probe["box_size"][axis] + tolerance
        for axis in range(3)
    )


def _valid_content(content):
    if content is None:
        return False
    if isinstance(content, (str, bytes, list, tuple, dict, set)):
        if len(content) == 0:
            return False
        if isinstance(content, dict) and content.get("valid") is False:
            return False
    return True


def _copy_content(content):
    if isinstance(content, dict):
        return dict(content)
    if isinstance(content, list):
        return list(content)
    if isinstance(content, tuple):
        return tuple(content)
    return content


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _weighted_content(items):
    """Blend scalar or vector captures; fall back to the strongest capture."""

    if not items:
        return 0.0

    if all(_is_number(value) for _, value in items):
        return float(sum(value * weight for weight, value in items))

    if all(
        isinstance(value, (tuple, list))
        and all(_is_number(part) for part in value)
        for _, value in items
    ):
        length = len(items[0][1])
        if all(len(value) == length for _, value in items):
            values = [
                sum(value[axis] * weight for weight, value in items)
                for axis in range(length)
            ]
            return tuple(values) if isinstance(items[0][1], tuple) else values

    return max(items, key=lambda item: item[0])[1]


class ReflectionProbeBlender:
    """Reflection probe capture state, normalized blending and parallax.

    Parallax convention: a probe yaw rotation of ``deg`` degrees transforms a
    world offset ``d`` into local space as ``(-dz, dy, dx)`` for 90 degrees and
    ``(dx, dy, dz)`` for 0 degrees, using a right-handed Y rotation.
    """

    def __init__(self, max_probes=8):
        if max_probes is not None:
            if isinstance(max_probes, bool) or not isinstance(max_probes, int):
                raise ProbeError("invalid_argument", {"argument": "max_probes"})
            if max_probes < 0:
                raise ProbeError("invalid_argument", {"argument": "max_probes"})
        self.max_probes = max_probes
        self._probes = {}
        self._active_tokens = {}
        self._token_states = {}
        self._token_sequence = 0
        self._device_state = "ok"

    def _require_probe(self, probe_id):
        probe = self._probes.get(probe_id)
        if probe is None:
            raise ProbeError("unknown_probe", {"probe_id": probe_id})
        return probe

    def _require_device(self):
        if self._device_state != "ok":
            raise ProbeError("device_lost")

    @staticmethod
    def _probe_state(probe):
        stale = probe["status"] == "dirty"
        return {
            "probe_id": probe["id"],
            "status": probe["status"],
            "stale": stale,
            "needs_recapture": stale,
            "version": probe["version"],
            "content": _copy_content(probe["content"]),
            "has_content": probe["has_content"],
            "resolution": probe["resolution"],
            "position": probe["position"],
            "rotation": probe["rotation"],
            "box_size": probe["box_size"],
            "indoor": probe["indoor"],
            "cull_characters": probe["cull_characters"],
            "characters_culled": probe["cull_characters"],
            "pending": probe["pending_token"] is not None,
            "device_valid": probe["device_valid"],
        }

    def _drop_token(self, token_id, state):
        token = self._active_tokens.pop(token_id, None)
        self._token_states[token_id] = state
        if token is not None:
            token["state"] = state
        return token

    def _drop_probe_tokens(self, probe, state):
        for token_id, token in list(self._active_tokens.items()):
            if token["probe_id"] == probe["id"]:
                self._drop_token(token_id, state)
        probe["pending_token"] = None

    def add_probe(self, probe_id, position=(0, 0, 0), rotation=(0, 0, 0),
                  box_size=(1, 1, 1), resolution=128, indoor=False):
        if probe_id is None:
            raise ProbeError("invalid_argument", {"argument": "probe_id"})
        if probe_id in self._probes:
            raise ProbeError("duplicate_probe", {"probe_id": probe_id})
        if self.max_probes is not None and len(self._probes) >= self.max_probes:
            raise ProbeError(
                "probe_limit",
                {
                    "probe_id": probe_id,
                    "limit": self.max_probes,
                    "active": len(self._probes),
                },
            )

        position = _vector(position, "position")
        rotation = _vector(rotation, "rotation")
        box_size = _vector(box_size, "box_size")
        if any(part <= 0.0 for part in box_size):
            raise ProbeError("invalid_argument", {"argument": "box_size"})
        if isinstance(resolution, bool) or not isinstance(resolution, int):
            raise ProbeError("invalid_argument", {"argument": "resolution"})
        if resolution <= 0:
            raise ProbeError("invalid_argument", {"argument": "resolution"})

        matrix = _rotation_matrix(rotation)
        probe = {
            "id": probe_id,
            "position": position,
            "rotation": rotation,
            "rotation_matrix": matrix,
            "inverse_rotation": _transpose(matrix),
            "box_size": box_size,
            "resolution": resolution,
            "indoor": bool(indoor),
            "content": None,
            "version": 0,
            "has_content": False,
            "status": "empty",
            "pending_token": None,
            "cull_characters": True,
            "device_valid": self._device_state == "ok",
            "order": len(self._probes),
        }
        self._probes[probe_id] = probe
        return self._probe_state(probe)

    def update_transform(self, probe_id, position=None, rotation=None):
        probe = self._require_probe(probe_id)
        changed = False
        if position is not None:
            position = _vector(position, "position")
            if position != probe["position"]:
                probe["position"] = position
                changed = True
        if rotation is not None:
            rotation = _vector(rotation, "rotation")
            if rotation != probe["rotation"]:
                probe["rotation"] = rotation
                matrix = _rotation_matrix(rotation)
                probe["rotation_matrix"] = matrix
                probe["inverse_rotation"] = _transpose(matrix)
                changed = True

        if changed:
            # A capture made with the old transform cannot be used as the
            # current box-projected reflection, although its pixels remain a
            # valid fallback until the replacement capture has completed.
            if probe["pending_token"] is not None:
                self._drop_probe_tokens(probe, "stale")
            probe["status"] = "dirty" if probe["has_content"] else "empty"

        result = self._probe_state(probe)
        result["updated"] = changed
        return result

    def remove_probe(self, probe_id):
        probe = self._require_probe(probe_id)
        self._drop_probe_tokens(probe, "cancelled")
        del self._probes[probe_id]
        return {"status": "removed", "probe_id": probe_id}

    def capture(self, probe_id, content, cull_characters=True, version=None):
        self._require_device()
        probe = self._require_probe(probe_id)
        if not _valid_content(content):
            raise ProbeError(
                "invalid_capture",
                {"probe_id": probe_id, "reason": "empty_content"},
            )

        if version is None:
            version = probe["version"] + 1
        else:
            if isinstance(version, bool) or not isinstance(version, int):
                raise ProbeError("invalid_argument", {"argument": "version"})
            if version < 0:
                raise ProbeError("invalid_argument", {"argument": "version"})
            if probe["has_content"] and version <= probe["version"]:
                return {
                    "status": "stale",
                    "probe_id": probe_id,
                    "version": probe["version"],
                }

        # A synchronous capture supersedes any in-flight readback.
        self._drop_probe_tokens(probe, "stale")
        probe["content"] = _copy_content(content)
        probe["version"] = version
        probe["has_content"] = True
        probe["status"] = "ready"
        probe["cull_characters"] = bool(cull_characters)
        probe["device_valid"] = self._device_state == "ok"
        probe["last_capture_cull_characters"] = bool(cull_characters)
        result = self._probe_state(probe)
        result["status"] = "captured"
        return result

    def begin_capture(self, probe_id, now=0):
        self._require_device()
        probe = self._require_probe(probe_id)

        if probe["pending_token"] is not None:
            self._drop_probe_tokens(probe, "stale")

        self._token_sequence += 1
        token_id = "{}:{}".format(probe_id, self._token_sequence)
        token = {
            "token": token_id,
            "probe_id": probe_id,
            "started_at": now,
            "base_version": probe["version"],
            "state": "pending",
        }
        self._active_tokens[token_id] = token
        self._token_states[token_id] = "pending"
        probe["pending_token"] = token_id
        probe["status"] = "pending"
        return dict(token)

    def finish_capture(self, token, content):
        token_id = token.get("token") if isinstance(token, dict) else None
        active = self._active_tokens.get(token_id)

        if active is None:
            state = self._token_states.get(token_id)
            if state is None and isinstance(token, dict):
                state = token.get("state")
            if state in ("stale", "superseded"):
                status = "stale"
            else:
                status = "cancelled"
            return {"status": status, "token": token_id}

        probe = self._probes.get(active["probe_id"])
        if probe is None or probe["pending_token"] != token_id:
            self._drop_token(token_id, "cancelled")
            return {"status": "cancelled", "token": token_id}

        if not _valid_content(content):
            self._drop_token(token_id, "invalid")
            probe["pending_token"] = None
            probe["status"] = "dirty" if probe["has_content"] else "empty"
            return {
                "status": "invalid",
                "token": token_id,
                "probe_id": active["probe_id"],
                "version": probe["version"],
            }

        if probe["version"] > active["base_version"]:
            self._drop_token(token_id, "stale")
            probe["pending_token"] = None
            probe["status"] = "ready" if probe["has_content"] else "empty"
            return {
                "status": "stale",
                "token": token_id,
                "probe_id": active["probe_id"],
                "version": probe["version"],
            }

        version = probe["version"] + 1
        probe["content"] = _copy_content(content)
        probe["version"] = version
        probe["has_content"] = True
        probe["status"] = "ready"
        probe["device_valid"] = self._device_state == "ok"
        self._drop_token(token_id, "captured")
        probe["pending_token"] = None
        result = self._probe_state(probe)
        result["status"] = "captured"
        result["token"] = token_id
        return result

    def cancel_capture(self, token):
        token_id = token.get("token") if isinstance(token, dict) else None
        active = self._active_tokens.get(token_id)
        if active is None:
            return {"status": "cancelled", "token": token_id}

        probe = self._probes.get(active["probe_id"])
        self._drop_token(token_id, "cancelled")
        if probe is not None and probe["pending_token"] == token_id:
            probe["pending_token"] = None
            probe["status"] = "dirty" if probe["has_content"] else "empty"
        return {"status": "cancelled", "token": token_id}

    def capture_state(self, probe_id):
        return self._probe_state(self._require_probe(probe_id))

    def _candidates(self, position):
        candidates = []
        for probe in self._probes.values():
            local = _world_to_local(position, probe)
            normalized = max(
                abs(local[axis]) / probe["box_size"][axis]
                for axis in range(3)
            )
            if normalized > 1.0 + 1e-9:
                continue
            distance_to_face = max(0.0, 1.0 - min(normalized, 1.0))
            # Keep an infinitesimal influence exactly on a box face so a
            # single containing probe still produces a normalized weight.
            weight = max(distance_to_face * distance_to_face, 1e-12)
            candidates.append((probe, weight, local))

        # An indoor volume owns reflections inside it.  This prevents an
        # overlapping outdoor probe from showing sky through an interior wall.
        indoor = [item for item in candidates if item[0]["indoor"]]
        if indoor:
            candidates = indoor
        return candidates

    def sample(self, position, camera=None):
        position = _vector(position, "position")
        if camera is not None:
            camera = _vector(camera, "camera")

        candidates = self._candidates(position)
        candidates.sort(
            key=lambda item: (-item[1], item[0]["order"], str(item[0]["id"]))
        )

        total = sum(item[1] for item in candidates)
        weights = {}
        raw_weights = {}
        for probe, weight, _ in candidates:
            raw_weights[probe["id"]] = weight
            weights[probe["id"]] = weight / total if total > 0.0 else 0.0

        content_items = [
            (weights[probe["id"]], probe["content"])
            for probe, _, _ in candidates
            if probe["has_content"]
        ]
        content_total = sum(weight for weight, _ in content_items)
        if content_total > 0.0:
            content_items = [
                (weight / content_total, content)
                for weight, content in content_items
            ]
        content = _weighted_content(content_items)

        sharpness = (
            sum(weights[probe["id"]] * probe["resolution"]
                for probe, _, _ in candidates)
            if total > 0.0 else 0.0
        )

        view_position = camera if camera is not None else position
        camera_probes = [
            probe for probe in self._probes.values()
            if _box_contains(view_position, probe)
        ]
        inside_probe = bool(camera_probes)
        primary = candidates[0][0] if candidates else None
        parallax_probe = primary
        if parallax_probe is None and camera_probes:
            parallax_probe = sorted(
                camera_probes, key=lambda probe: (probe["order"], str(probe["id"]))
            )[0]

        if parallax_probe is None:
            parallax_local = (0.0, 0.0, 0.0)
        else:
            parallax_local = _world_to_local(view_position, parallax_probe)

        # Box projection is clamped away from a zero denominator.  Without
        # this a camera crossing the probe origin reverses the correction.
        projected = list(parallax_local)
        for axis in range(3):
            if parallax_probe is not None and abs(projected[axis]) > 1e-12:
                limit = parallax_probe["box_size"][axis] / abs(projected[axis])
                if limit < 1.0:
                    projected[axis] *= limit

        result = {
            "content": content,
            "weights": weights,
            "raw_weights": raw_weights,
            "normalized": total > 0.0,
            "sharpness": sharpness,
            "parallax_local": parallax_local,
            "parallax_projected": tuple(projected),
            "inside_probe": inside_probe,
            "probe_id": primary["id"] if primary is not None else None,
        }
        if primary is not None:
            result["resolution"] = primary["resolution"]
            result["indoor"] = primary["indoor"]
            result["version"] = primary["version"]
            result["status"] = primary["status"]
            result["stale"] = primary["status"] == "dirty"
        else:
            result["version"] = 0
            result["status"] = "empty"
            result["stale"] = False
        return result

    def device_lost(self):
        self._device_state = "lost"
        for probe in self._probes.values():
            probe["device_valid"] = False
            if probe["pending_token"] is not None:
                self._drop_probe_tokens(probe, "cancelled")
                probe["status"] = "dirty" if probe["has_content"] else "empty"
        return {"status": "lost"}

    def device_restored(self):
        self._device_state = "ok"
        for probe in self._probes.values():
            # Keep the last complete CPU-side capture as the fallback.  The
            # next successful capture replaces it; a lost device must not turn
            # every reflection into an empty (black) texture.
            probe["device_valid"] = True
            if probe["has_content"] and probe["status"] == "empty":
                probe["status"] = "ready"
        return {"status": "restored", "probes": list(self._probes)}

    def device_state(self):
        return self._device_state

    def callbacks(self):
        return [
            {
                "token": token["token"],
                "probe_id": token["probe_id"],
                "started_at": token["started_at"],
                "status": "pending",
            }
            for token in self._active_tokens.values()
        ]
