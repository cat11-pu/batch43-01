import threading
import time
from dataclasses import dataclass, field


class ResourceInUseError(RuntimeError):
    """Raised when a resource is recycled before its fence completes."""


class FenceError(ValueError):
    """Raised when a frame command operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _Frame:
    index: int
    token: str
    generation: int
    sequence: int
    status: str = "recorded"
    submitted_at: float | None = None
    resources: set = field(default_factory=set)


class FrameCommandFence:
    """Per-frame command recording, submission order and fence reuse."""

    _ACTIVE = frozenset(("recorded", "submitted"))

    def __init__(self, frames_in_flight=2):
        if not isinstance(frames_in_flight, int) or frames_in_flight < 1:
            raise ValueError("frames_in_flight must be a positive integer")

        self.frames_in_flight = frames_in_flight
        self._generation = 0
        self._device = "ok"
        self._width = None
        self._height = None
        self._shutting_down = False

        self._frames = {}
        self._resources = {}
        self._next_sequence = 0
        self._next_submit_sequence = 0

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)

    def begin(self, frame_index):
        with self._lock:
            self._ensure_accepting("begin")

            previous = self._frames.get(frame_index)
            if previous is not None:
                if previous.status in self._ACTIVE:
                    raise FenceError(
                        "frame_in_use",
                        "frame {} is already being recorded or submitted".format(
                            frame_index
                        ),
                    )
                self._retire_frame(previous)

            active = self._active_frames()
            if len(active) >= self.frames_in_flight:
                raise FenceError(
                    "too_many_frames",
                    "{} frames are already in flight".format(len(active)),
                )

            sequence = self._next_sequence
            self._next_sequence += 1
            token = "g{}:f{}:s{}".format(
                self._generation, frame_index, sequence
            )
            frame = _Frame(
                index=frame_index,
                token=token,
                generation=self._generation,
                sequence=sequence,
            )
            self._frames[frame_index] = frame
            return {
                "status": "recording",
                "frame_index": frame_index,
                "token": token,
                "generation": self._generation,
                "sequence": sequence,
            }

    def submit(self, frame_index, token=None, resources=()):
        with self._lock:
            self._ensure_accepting("submit")

            frame = self._frames.get(frame_index)
            if frame is None:
                raise FenceError(
                    "not_recorded", "frame {} was not recorded".format(frame_index)
                )
            if frame.status == "submitted":
                raise FenceError(
                    "already_submitted", "frame {} was already submitted".format(
                        frame_index
                    )
                )
            if frame.status == "signaled":
                raise FenceError(
                    "already_submitted",
                    "frame {} has already completed".format(frame_index),
                )
            if frame.status != "recorded":
                raise FenceError(
                    "invalid_state",
                    "frame {} is {}".format(frame_index, frame.status),
                )

            if token is not None and token != frame.token:
                token_generation = self._token_generation(token)
                if (
                    token_generation is not None
                    and token_generation != self._generation
                ):
                    raise FenceError(
                        "stale_generation",
                        "token belongs to generation {}".format(token_generation),
                    )
                raise FenceError(
                    "token_mismatch",
                    "token does not belong to frame {}".format(frame_index),
                )

            if frame.sequence != self._next_submit_sequence:
                raise FenceError(
                    "out_of_order",
                    "expected sequence {}, got {}".format(
                        self._next_submit_sequence, frame.sequence
                    ),
                )

            claimed = self._normalise_resources(resources)
            conflicts = []
            for resource in claimed:
                owner = self._resources.get(resource)
                if (
                    owner is not None
                    and owner is not frame
                    and owner.status in self._ACTIVE
                ):
                    conflicts.append(resource)
            if conflicts:
                raise FenceError(
                    "resource_in_use",
                    "resources are still owned by in-flight frames: {}".format(
                        sorted(map(str, conflicts))
                    ),
                )

            for resource in claimed:
                self._resources[resource] = frame
                frame.resources.add(resource)

            frame.status = "submitted"
            frame.submitted_at = time.monotonic()
            self._next_submit_sequence += 1
            return {
                "status": "submitted",
                "frame_index": frame_index,
                "token": frame.token,
                "generation": self._generation,
                "sequence": frame.sequence,
            }

    def signal(self, frame_index):
        with self._lock:
            frame = self._frames.get(frame_index)
            if frame is None:
                raise FenceError(
                    "not_recorded", "frame {} was not recorded".format(frame_index)
                )
            if frame.status == "recorded":
                raise FenceError(
                    "not_submitted", "frame {} has not been submitted".format(
                        frame_index
                    )
                )
            if frame.status == "cancelled":
                raise FenceError(
                    "cancelled", "frame {} was cancelled".format(frame_index)
                )

            if frame.status != "signaled":
                frame.status = "signaled"
                self._condition.notify_all()

            return {
                "status": "signaled",
                "frame_index": frame_index,
                "token": frame.token,
                "generation": frame.generation,
            }

    def wait(self, frame_index, timeout=0):
        with self._condition:
            frame = self._frames.get(frame_index)
            if frame is None:
                raise FenceError(
                    "not_recorded", "frame {} was not recorded".format(frame_index)
                )
            if frame.status == "recorded":
                raise FenceError(
                    "not_submitted", "frame {} has not been submitted".format(
                        frame_index
                    )
                )
            if frame.status == "cancelled":
                raise FenceError(
                    "cancelled", "frame {} was cancelled".format(frame_index)
                )

            if timeout is None:
                while frame.status == "submitted":
                    self._condition.wait()
            elif timeout > 0:
                deadline = time.monotonic() + timeout
                while frame.status == "submitted":
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)

            if frame.status == "submitted":
                return {
                    "status": "timeout",
                    "frame_index": frame_index,
                    "token": frame.token,
                    "generation": frame.generation,
                }
            if frame.status == "cancelled":
                raise FenceError(
                    "cancelled", "frame {} was cancelled".format(frame_index)
                )
            return {
                "status": "complete",
                "frame_index": frame_index,
                "token": frame.token,
                "generation": frame.generation,
            }

    def acquire(self, resource, frame_index):
        with self._lock:
            frame = self._frames.get(frame_index)
            if frame is None:
                raise FenceError(
                    "not_recorded", "frame {} was not recorded".format(frame_index)
                )
            if frame.status in ("cancelled", "signaled"):
                raise FenceError(
                    "frame_complete",
                    "frame {} is {}".format(frame_index, frame.status),
                )

            owner = self._resources.get(resource)
            if owner is frame:
                return {
                    "status": "acquired",
                    "resource": resource,
                    "frame_index": frame_index,
                    "token": frame.token,
                }
            if owner is not None and owner.status in self._ACTIVE:
                raise FenceError(
                    "resource_in_use",
                    "resource is owned by frame {}".format(owner.index),
                )

            self._resources[resource] = frame
            frame.resources.add(resource)
            return {
                "status": "acquired",
                "resource": resource,
                "frame_index": frame_index,
                "token": frame.token,
            }

    def recycle(self, resource):
        with self._lock:
            owner = self._resources.get(resource)
            if owner is not None:
                if owner.status in self._ACTIVE:
                    raise ResourceInUseError(
                        "resource is still in use by frame {}".format(owner.index)
                    )
                del self._resources[resource]
                owner.resources.discard(resource)

            return {
                "status": "recycled",
                "resource": resource,
                "frame_index": owner.index if owner is not None else None,
            }

    def resize(self, width, height):
        with self._lock:
            if self._shutting_down:
                raise FenceError("shutting_down", "fence is shutting down")

            completed, cancelled = self._cancel_all_frames(notify=True)
            self._generation += 1
            self._width = width
            self._height = height
            self._reset_submission_order()
            return {
                "status": "resized",
                "generation": self._generation,
                "width": self._width,
                "height": self._height,
                "completed": completed,
                "cancelled": cancelled,
            }

    def device_lost(self):
        with self._lock:
            completed, cancelled = self._cancel_all_frames(notify=True)
            self._generation += 1
            self._device = "lost"
            self._reset_submission_order()
            return {
                "status": "lost",
                "generation": self._generation,
                "completed": completed,
                "cancelled": cancelled,
            }

    def device_restored(self, width=None, height=None):
        with self._lock:
            if self._shutting_down:
                raise FenceError("shutting_down", "fence is shutting down")

            completed, cancelled = self._cancel_all_frames(notify=True)
            if width is not None:
                self._width = width
            if height is not None:
                self._height = height
            self._generation += 1
            self._device = "ok"
            self._reset_submission_order()
            return {
                "status": "restored",
                "generation": self._generation,
                "width": self._width,
                "height": self._height,
                "completed": completed,
                "cancelled": cancelled,
            }

    def shutdown(self, timeout=0):
        with self._condition:
            if self._shutting_down:
                return {"completed": [], "cancelled": [], "status": "shutdown"}

            if timeout is None:
                while any(
                    frame.status == "submitted"
                    for frame in self._frames.values()
                ):
                    self._condition.wait()
            elif timeout > 0:
                deadline = time.monotonic() + timeout
                while any(
                    frame.status == "submitted"
                    for frame in self._frames.values()
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)

            completed = []
            cancelled = []
            for frame in sorted(
                self._frames.values(), key=lambda item: item.sequence
            ):
                if frame.status == "signaled":
                    completed.append(frame.index)
                else:
                    frame.status = "cancelled"
                    cancelled.append(frame.index)

            self._condition.notify_all()
            self._release_all_resources()
            self._frames.clear()
            self._shutting_down = True
            self._reset_submission_order()
            return {
                "status": "shutdown",
                "completed": completed,
                "cancelled": cancelled,
            }

    def pending(self):
        with self._lock:
            return self._pending_locked()

    def state(self):
        with self._lock:
            return {
                "generation": self._generation,
                "device": self._device,
                "width": self._width,
                "height": self._height,
                "frames_in_flight": self.frames_in_flight,
                "in_flight": len(self._active_frames()),
                "pending": self._pending_locked(),
                "resources": len(self._resources),
                "shutdown": self._shutting_down,
            }

    def _ensure_accepting(self, operation):
        if self._shutting_down:
            raise FenceError(
                "shutting_down", "cannot {} while shutting down".format(operation)
            )
        if self._device != "ok":
            raise FenceError(
                "device_lost", "cannot {} while device is lost".format(operation)
            )

    def _active_frames(self):
        return [
            frame
            for frame in self._frames.values()
            if frame.status in self._ACTIVE
        ]

    def _pending_locked(self):
        submitted = [
            frame
            for frame in self._frames.values()
            if frame.status == "submitted"
        ]
        submitted.sort(key=lambda item: item.sequence)
        return [frame.index for frame in submitted]

    def _normalise_resources(self, resources):
        if resources is None:
            return []
        if isinstance(resources, (str, bytes)):
            resources = (resources,)

        result = []
        seen = set()
        for resource in resources:
            if resource in seen:
                continue
            seen.add(resource)
            result.append(resource)
        return result

    def _retire_frame(self, frame):
        if self._frames.get(frame.index) is frame:
            del self._frames[frame.index]
        self._release_frame_resources(frame)

    def _release_frame_resources(self, frame):
        for resource in list(frame.resources):
            if self._resources.get(resource) is frame:
                del self._resources[resource]
        frame.resources.clear()

    def _release_all_resources(self):
        for frame in self._frames.values():
            frame.resources.clear()
        self._resources.clear()

    def _cancel_all_frames(self, notify=False):
        completed = []
        cancelled = []
        for frame in sorted(
            self._frames.values(), key=lambda item: item.sequence
        ):
            if frame.status == "signaled":
                completed.append(frame.index)
            else:
                frame.status = "cancelled"
                cancelled.append(frame.index)
            self._release_frame_resources(frame)

        if notify and cancelled:
            self._condition.notify_all()
        self._frames.clear()
        return completed, cancelled

    def _reset_submission_order(self):
        self._next_submit_sequence = self._next_sequence

    @staticmethod
    def _token_generation(token):
        if not isinstance(token, str) or not token.startswith("g"):
            return None
        generation = token[1:].split(":", 1)[0]
        try:
            return int(generation)
        except ValueError:
            return None
