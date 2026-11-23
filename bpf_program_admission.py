"""BPF program admission and tenant isolation."""

import threading


class BpfError(Exception):
    """Configuration failure raised by the admission controller."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class BpfProgramAdmission:
    """Validates helper usage, map pins and tail calls before loading BPF."""

    BPF_FS_ROOT = "/sys/fs/bpf"

    def __init__(
        self,
        tenant="tenant-a",
        allowed_helpers=None,
        map_capacity=64,
        cgroup_root="/sys/fs/cgroup/tenant-a",
    ):
        self.tenant = tenant
        self.allowed_helpers = allowed_helpers or {}
        self.map_capacity = map_capacity
        self.cgroup_root = cgroup_root.rstrip("/")
        self._lock = threading.Lock()
        self._programs = {}
        self._pin_paths = {}
        self._map_refs = {}
        self._audit = []
        self._counters = {"loaded": 0, "rejected": 0, "unloaded": 0}

    def _tenant_pin_root(self):
        return "%s/%s/" % (self.BPF_FS_ROOT, self.tenant)

    def _reject(self, program, code, error, now):
        self._audit.append(
            {
                "tenant": program.get("tenant"),
                "type": program.get("type"),
                "pin_path": program.get("pin_path"),
                "ok": False,
                "code": code,
                "now": now,
            }
        )
        self._counters["rejected"] += 1
        return {
            "ok": False,
            "error": error,
            "code": code,
            "validator_log": "verifier: %s: %s" % (code, error),
        }

    def load(self, program, now=0.0):
        if program.get("tenant") != self.tenant:
            return self._reject(program, "BPF_TENANT_MISMATCH", "EPERM", now)

        allowed = self.allowed_helpers.get(program.get("type"), set())
        for helper in program.get("helpers") or []:
            if helper not in allowed:
                return self._reject(program, "BPF_HELPER_DENIED", "EPERM", now)

        if program.get("sleepable"):
            return self._reject(program, "BPF_SLEEPABLE_DENIED", "EPERM", now)

        if not program.get("license"):
            return self._reject(program, "BPF_LICENSE_DENIED", "EINVAL", now)

        if not program.get("privileged", True):
            return self._reject(program, "BPF_UNPRIVILEGED", "EPERM", now)

        if program.get("map_capacity") != self.map_capacity:
            return self._reject(program, "EINVAL", "EINVAL", now)

        pin_path = program.get("pin_path") or ""
        if not pin_path.startswith(self._tenant_pin_root()):
            return self._reject(program, "BPF_MAP_TENANT_MISMATCH", "EPERM", now)

        attach_cgroup = program.get("attach_cgroup") or ""
        if attach_cgroup != self.cgroup_root and not attach_cgroup.startswith(
            self.cgroup_root + "/"
        ):
            return self._reject(program, "BPF_CGROUP_ESCAPE", "EPERM", now)

        for target in program.get("tail_calls") or []:
            if target.get("tenant") != self.tenant:
                return self._reject(program, "BPF_TAILCALL_TENANT", "EPERM", now)

        program_id = program.get("id")
        with self._lock:
            if pin_path in self._pin_paths:
                return self._reject(program, "BPF_PIN_EXISTS", "EEXIST", now)
            self._pin_paths[pin_path] = program_id

        if not program.get("attach_ok", True):
            with self._lock:
                self._pin_paths.pop(pin_path, None)
            result = self._reject(program, "BPF_ATTACH_FAILED", "EIO", now)
            result["cleanup"] = True
            return result

        with self._lock:
            self._programs[program_id] = {
                "id": program_id,
                "tenant": program.get("tenant"),
                "type": program.get("type"),
                "pin_path": pin_path,
                "maps_used": list(program.get("maps_used") or []),
            }
            for map_path in program.get("maps_used") or []:
                self._map_refs.setdefault(map_path, set()).add(program_id)
        self._audit.append(
            {
                "tenant": program.get("tenant"),
                "type": program.get("type"),
                "pin_path": pin_path,
                "ok": True,
                "code": None,
                "now": now,
            }
        )
        self._counters["loaded"] += 1
        return {
            "ok": True,
            "error": None,
            "code": None,
            "validator_log": "verifier: OK",
            "cleanup": False,
        }

    def unload(self, program_id):
        with self._lock:
            record = self._programs.get(program_id)
            if record is None:
                return {"ok": True, "error": None}
            for map_path in record["maps_used"]:
                refs = self._map_refs.get(map_path, set())
                if refs - {program_id}:
                    return {"ok": False, "error": "EBUSY"}
            for map_path in record["maps_used"]:
                refs = self._map_refs.get(map_path)
                if refs is not None:
                    refs.discard(program_id)
                    if not refs:
                        del self._map_refs[map_path]
            self._pin_paths.pop(record["pin_path"], None)
            del self._programs[program_id]
        self._counters["unloaded"] += 1
        return {"ok": True, "error": None}

    def attach(self, program_id):
        if program_id not in self._programs:
            return {"ok": False, "error": "ENOENT"}
        return {"ok": True, "error": None}

    def pin_path(self, program_id):
        record = self._programs.get(program_id)
        return record["pin_path"] if record else None

    def loaded(self):
        return sorted(self._programs)

    def counters(self):
        return dict(self._counters, audit=list(self._audit))
