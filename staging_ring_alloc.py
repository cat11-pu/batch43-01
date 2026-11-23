import itertools
import threading


class StagingError(ValueError):
    """Raised when a staging ring operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RingExhausted(StagingError):
    """Raised when no safe range can be allocated in the ring."""


class MisalignedUpload(StagingError):
    """Raised when a requested alignment violates the resource minimum."""


class StagingRingAlloc:
    """Frame-scoped staging ring with fence aware reuse and generations."""

    KIND_ALIGNMENT = {
        "buffer": 4,
        "texture": 16,
        "constant": 256,
    }

    def __init__(self, capacity=1024, wait_timeout=0.05):
        self.capacity = capacity
        self.wait_timeout = wait_timeout
        self.generation = 1
        self.device_lost_flag = False
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._id_counter = itertools.count(1)
        self._free = [(0, capacity)]
        self._allocations = {}
        self._covered = bytearray(capacity)
        self._high_water = 0
        self._barrier_frames = set()

    def _round_up(self, value, alignment):
        return (value + alignment - 1) // alignment * alignment

    def _find_slot(self, size, alignment):
        for index, (start, end) in enumerate(self._free):
            aligned_start = self._round_up(start, alignment)
            if aligned_start + size <= end:
                return index, start, end, aligned_start
        return None

    def _resolve(self, marker):
        marker_id = marker.get("id")
        record = self._allocations.get(marker_id)
        if record is None or record["generation"] != marker.get("generation"):
            raise StagingError("stale_marker", marker)
        if record["generation"] != self.generation:
            raise StagingError("stale_marker", marker)
        return record

    def alloc(self, size, kind="buffer", frame=0, alignment=None, wait=False):
        minimum = self.KIND_ALIGNMENT[kind]
        requested = minimum if alignment is None else alignment
        if requested < minimum:
            raise MisalignedUpload("misaligned_upload", {
                "kind": kind,
                "requested": requested,
                "minimum": minimum,
            })
        if size <= 0:
            raise StagingError("invalid_size", {"size": size})
        if size > self.capacity:
            raise RingExhausted("ring_exhausted", {
                "size": size,
                "capacity": self.capacity,
            })

        with self._cond:
            while True:
                slot = self._find_slot(size, requested)
                if slot is not None:
                    index, start, end, aligned_start = slot
                    del self._free[index]
                    if start < aligned_start:
                        self._free.append((start, aligned_start))
                    if aligned_start + size < end:
                        self._free.append((aligned_start + size, end))
                    self._free.sort()

                    marker_id = next(self._id_counter)
                    marker = {
                        "id": marker_id,
                        "offset": aligned_start,
                        "size": size,
                        "kind": kind,
                        "frame": frame,
                        "alignment": requested,
                        "generation": self.generation,
                        "version": 0,
                    }
                    self._allocations[marker_id] = {
                        "marker": marker,
                        "frame": frame,
                        "start": aligned_start,
                        "end": aligned_start + size,
                        "covered": 0,
                        "completed": False,
                        "generation": self.generation,
                        "data": bytearray(size),
                    }
                    if aligned_start < self._high_water:
                        self._barrier_frames.add(frame)
                    if aligned_start + size > self._high_water:
                        self._high_water = aligned_start + size
                    return dict(marker)

                if not wait:
                    raise RingExhausted("ring_exhausted", {
                        "size": size,
                        "frame": frame,
                    })
                if not self._cond.wait(timeout=self.wait_timeout):
                    raise RingExhausted("ring_exhausted", {
                        "size": size,
                        "frame": frame,
                        "wait": True,
                    })

    def write(self, marker, data, sub_offset=0, partial=False):
        with self._lock:
            record = self._resolve(marker)
            end = sub_offset + len(data)
            if sub_offset < 0 or end > record["marker"]["size"]:
                raise StagingError("out_of_range", {
                    "sub_offset": sub_offset,
                    "length": len(data),
                    "size": record["marker"]["size"],
                })

            record["data"][sub_offset:end] = data
            start = record["start"] + sub_offset
            stop = record["start"] + end
            was_complete = record["covered"] == record["marker"]["size"]
            newly_covered = sum(
                1 for flag in self._covered[start:stop] if flag == 0
            )
            self._covered[start:stop] = b"\x01" * (stop - start)
            record["covered"] += newly_covered

            whole_range = record["covered"] == record["marker"]["size"]
            if whole_range and not was_complete:
                record["marker"]["version"] += 1
            return {"version": record["marker"]["version"]}

    def read(self, marker):
        with self._lock:
            record = self._resolve(marker)
            return bytes(record["data"])

    def flush(self, frame):
        with self._lock:
            barrier = frame in self._barrier_frames
            self._barrier_frames.discard(frame)
            pending = [
                record["marker"]["size"]
                for record in self._allocations.values()
                if record["frame"] == frame
            ]
            return {
                "frame": frame,
                "barrier": barrier,
                "bytes": sum(pending),
                "allocations": len(pending),
                "generation": self.generation,
            }

    def recycle(self, frame):
        with self._cond:
            frame_records = [
                record
                for record in self._allocations.values()
                if record["frame"] == frame
            ]
            if any(not record["completed"] for record in frame_records):
                raise StagingError("fence_pending", {"frame": frame})

            freed = 0
            for record in frame_records:
                freed += self._release(record)
            self._cond.notify_all()
            return {"frame": frame, "freed": freed}

    def _release(self, record):
        marker_id = record["marker"]["id"]
        if marker_id not in self._allocations:
            return 0
        start, end = record["start"], record["end"]
        del self._allocations[marker_id]
        self._covered[start:end] = b"\x00" * (end - start)

        self._free.append((start, end))
        self._free.sort()
        merged = []
        for interval in self._free:
            if merged and merged[-1][1] >= interval[0]:
                previous = merged.pop()
                merged.append((previous[0], max(previous[1], interval[1])))
            else:
                merged.append(interval)
        self._free = merged
        return end - start

    def complete(self, marker):
        with self._cond:
            record = self._resolve(marker)
            if record["covered"] != record["marker"]["size"]:
                raise StagingError("incomplete_upload", {
                    "covered": record["covered"],
                    "size": record["marker"]["size"],
                })
            record["completed"] = True
            self._cond.notify_all()
            return True

    def state(self):
        with self._lock:
            used = sum(
                record["end"] - record["start"]
                for record in self._allocations.values()
            )
            return {
                "capacity": self.capacity,
                "used_bytes": used,
                "free_bytes": self.capacity - used,
                "generation": self.generation,
                "device_lost": self.device_lost_flag,
                "live_allocations": len(self._allocations),
            }

    def device_lost(self):
        with self._cond:
            self.device_lost_flag = True
            self._cond.notify_all()
            return True

    def device_restored(self):
        with self._cond:
            self.generation += 1
            self.device_lost_flag = False
            self._free = [(0, self.capacity)]
            self._allocations = {}
            self._covered = bytearray(self.capacity)
            self._high_water = 0
            self._barrier_frames.clear()
            self._cond.notify_all()
            return self.generation
