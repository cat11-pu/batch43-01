"""fanotify verdict dispatcher with fail-closed queue handling."""

import threading


class FanotifyError(Exception):
    """Configuration failure raised by the dispatcher."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class FanotifyVerdictDispatcher:
    """Coordinates file-access verdicts and survives queue overflow safely."""

    def __init__(self, queue_capacity=8, scan_timeout=5.0):
        self.queue_capacity = queue_capacity
        self.scan_timeout = scan_timeout
        self._lock = threading.Lock()
        self._verdicts = {}
        self._inflight = {}
        self._pending = []
        self._handles = {}
        self._marks = set()
        self._scan_counts = {}
        self._generations = {}
        self._paths = {}
        self._overflowed = False
        self._scan_seq = 0
        self._counters = {"queue_overflow": 0, "denied": 0}

    def _deny(self, code, node_id, generation, verdict=None):
        self._counters["denied"] += 1
        return {
            "action": "deny",
            "code": code,
            "verdict": verdict,
            "scanned": False,
            "node_id": node_id,
            "generation": generation,
        }

    def _register_handle(self, node_id, pid, pid_start):
        handles = self._handles.setdefault(node_id, {})
        key = (pid, pid_start)
        handles[key] = handles.get(key, 0) + 1
        self._marks.add(node_id)

    def _drop_node_verdicts(self, node_id, keep_generation=None):
        for key in list(self._verdicts):
            if key[0] == node_id and key[1] != keep_generation:
                del self._verdicts[key]

    def open_event(
        self,
        node_id,
        path,
        pid=100,
        pid_start=1,
        generation=1,
        executable=False,
        deleted=False,
        scan_result="allow",
        in_flight=False,
        now=0.0,
    ):
        key = (node_id, generation)
        with self._lock:
            if deleted:
                return self._deny("FANOTIFY_DELETED", node_id, generation)
            if self._overflowed:
                return self._deny("FANOTIFY_QUEUE_OVERFLOW", node_id, generation)
            self._paths[node_id] = path
            self._generations.setdefault(node_id, generation)
            cached = self._verdicts.get(key)
            if cached is not None and self._generations[node_id] == generation:
                if cached["action"] == "allow":
                    self._register_handle(node_id, pid, pid_start)
                return dict(cached, scanned=False)
            entry = self._inflight.get(key)
            if entry is None:
                if len(self._pending) >= self.queue_capacity:
                    self._overflowed = True
                    self._counters["queue_overflow"] += 1
                    return self._deny("FANOTIFY_QUEUE_OVERFLOW", node_id, generation)
                self._scan_seq += 1
                batch = "batch-%d" % self._scan_seq
                entry = {"event": threading.Event(), "result": None, "batch": batch}
                self._inflight[key] = entry
                self._pending.append(key)
                self._scan_counts[node_id] = self._scan_counts.get(node_id, 0) + 1
                leader = True
            else:
                leader = False

        if not leader:
            entry["event"].wait()
            result = entry["result"]
            with self._lock:
                if result["action"] == "allow":
                    self._register_handle(node_id, pid, pid_start)
            return dict(result, scanned=False)

        if in_flight:
            return {
                "action": "pending",
                "code": None,
                "verdict": entry["batch"],
                "scanned": True,
                "node_id": node_id,
                "generation": generation,
            }

        if scan_result == "allow":
            result = {
                "action": "allow",
                "code": None,
                "verdict": entry["batch"],
                "node_id": node_id,
                "generation": generation,
            }
        elif scan_result == "timeout":
            result = {
                "action": "deny",
                "code": "FANOTIFY_SCAN_TIMEOUT",
                "verdict": entry["batch"],
                "node_id": node_id,
                "generation": generation,
            }
        else:
            result = {
                "action": "deny",
                "code": "FANOTIFY_SCAN_DENIED",
                "verdict": entry["batch"],
                "node_id": node_id,
                "generation": generation,
            }

        with self._lock:
            self._inflight.pop(key, None)
            if key in self._pending:
                self._pending.remove(key)
            if self._generations.get(node_id, generation) != generation:
                result = {
                    "action": "deny",
                    "code": "FANOTIFY_GENERATION_MISMATCH",
                    "verdict": entry["batch"],
                    "node_id": node_id,
                    "generation": generation,
                }
            else:
                self._verdicts[key] = dict(result)
                if result["action"] == "allow":
                    self._register_handle(node_id, pid, pid_start)
            if result["action"] == "deny":
                self._counters["denied"] += 1
            entry["result"] = result
            entry["event"].set()
        return dict(result, scanned=True)

    def close_handle(self, node_id, pid=100, pid_start=1):
        with self._lock:
            handles = self._handles.get(node_id)
            if not handles:
                return
            key = (pid, pid_start)
            if key in handles:
                handles[key] -= 1
                if handles[key] <= 0:
                    del handles[key]
            if not handles:
                del self._handles[node_id]
                self._marks.discard(node_id)
                self._drop_node_verdicts(node_id)

    def verdict(self, node_id, generation=1):
        with self._lock:
            cached = self._verdicts.get((node_id, generation))
            if cached is None:
                return None
            return cached["action"]

    def overflow(self, count=1):
        with self._lock:
            self._overflowed = True
            self._counters["queue_overflow"] += count

    def rename(self, node_id, new_path, generation=2):
        with self._lock:
            self._paths[node_id] = new_path
            self._generations[node_id] = generation
            self._drop_node_verdicts(node_id, keep_generation=generation)

    def restart(self):
        with self._lock:
            denied_pending = len(self._pending)
            for (node_id, generation), entry in self._inflight.items():
                entry["result"] = {
                    "action": "deny",
                    "code": "FANOTIFY_SERVICE_RESTART",
                    "verdict": entry["batch"],
                    "node_id": node_id,
                    "generation": generation,
                }
                entry["event"].set()
            self._inflight.clear()
            self._pending.clear()
            self._verdicts.clear()
            self._handles.clear()
            self._marks.clear()
            self._overflowed = False
        return {"denied_pending": denied_pending}

    def pending(self):
        with self._lock:
            return [node_id for node_id, _ in self._pending]

    def mount_mark(self, node_id):
        with self._lock:
            return node_id in self._marks

    def scan_count(self, node_id):
        with self._lock:
            return self._scan_counts.get(node_id, 0)

    def counters(self):
        with self._lock:
            return dict(self._counters)
