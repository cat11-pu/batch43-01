"""cgroup pids controller."""

import threading


class CgroupError(Exception):
    """Error carrying a stable cgroup error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CgroupPidController:
    """pids.max accounting across one hierarchy."""

    def __init__(self, hierarchy="unified"):
        self.hierarchy = hierarchy
        self._lock = threading.RLock()
        self._limits = {}
        self._thread_mode = {}
        self._events = {}
        self._disabled = set()
        self._own_pids = {}
        self._own_threads = {}
        self._pid_cgroup = {}
        self._zombies = set()
        self._next_pid = 1
        self._next_tid = 1

    @staticmethod
    def _norm(path):
        if not path:
            return "/"
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/")
        return path

    @staticmethod
    def _ancestors(cgroup):
        chain = []
        while True:
            chain.append(cgroup)
            if cgroup == "/":
                return chain
            cgroup = cgroup.rsplit("/", 1)[0] or "/"

    @staticmethod
    def _in_subtree(path, root):
        return root == "/" or path == root or path.startswith(root + "/")

    def _aggregate_pids(self, cgroup):
        return sum(
            len(pids)
            for path, pids in self._own_pids.items()
            if self._in_subtree(path, cgroup)
        )

    def _aggregate_threads(self, cgroup):
        return sum(
            count
            for path, count in self._own_threads.items()
            if self._in_subtree(path, cgroup)
        )

    def _aggregate_charge(self, cgroup):
        charge = 0
        for path, pids in self._own_pids.items():
            if self._in_subtree(path, cgroup):
                charge += len(pids)
                if self._thread_mode.get(path, False):
                    charge += self._own_threads.get(path, 0)
        return charge

    def _check_limit_locked(self, cgroup, extra=1):
        for ancestor in self._ancestors(cgroup):
            if ancestor in self._disabled:
                continue
            limit = self._limits.get(ancestor)
            if limit is not None and self._aggregate_charge(ancestor) + extra > limit:
                self._events[ancestor] = self._events.get(ancestor, 0) + 1
                raise CgroupError("EAGAIN", "pids.max reached at %s" % ancestor)

    def set_limit(self, cgroup, limit):
        """Set pids.max for a cgroup; returns {"cgroup": str, "limit": int}."""
        cgroup = self._norm(cgroup)
        with self._lock:
            self._limits[cgroup] = limit
        return {"cgroup": cgroup, "limit": limit}

    def fork(self, cgroup):
        """Fork a process under a cgroup.

        At the limit raise CgroupError("EAGAIN").  Returns {"pid": int,
        "pids": int} with the aggregate count.
        """
        cgroup = self._norm(cgroup)
        with self._lock:
            self._check_limit_locked(cgroup)
            pid = self._next_pid
            self._next_pid += 1
            self._own_pids.setdefault(cgroup, set()).add(pid)
            self._pid_cgroup[pid] = cgroup
            return {"pid": pid, "pids": self._aggregate_charge(cgroup)}

    def spawn_thread(self, cgroup):
        """Spawn a thread into a cgroup; enforces thread-mode limits."""
        cgroup = self._norm(cgroup)
        with self._lock:
            if self._thread_mode.get(cgroup, False):
                self._check_limit_locked(cgroup)
            tid = self._next_tid
            self._next_tid += 1
            self._own_threads[cgroup] = self._own_threads.get(cgroup, 0) + 1
            return {"tid": tid, "tasks": self.tasks(cgroup)}

    def pids(self, cgroup):
        """Return the aggregate process count including descendants."""
        cgroup = self._norm(cgroup)
        with self._lock:
            return self._aggregate_pids(cgroup)

    def tasks(self, cgroup):
        """Return the aggregate task count including threads."""
        cgroup = self._norm(cgroup)
        with self._lock:
            return self._aggregate_pids(cgroup) + self._aggregate_threads(cgroup)

    def set_thread_mode(self, cgroup, enabled):
        """Count threads against the limit when thread mode is on."""
        cgroup = self._norm(cgroup)
        with self._lock:
            self._thread_mode[cgroup] = bool(enabled)
        return {"cgroup": cgroup, "thread_mode": bool(enabled)}

    def migrate(self, pid, src, dst):
        """Move a process atomically.

        Either cgroup is over its limit during the move raises
        CgroupError("EAGAIN"); there must be no intermediate double count.
        """
        src = self._norm(src)
        dst = self._norm(dst)
        with self._lock:
            if self._pid_cgroup.get(pid) != src:
                raise CgroupError("ENOENT", "pid %s not in %s" % (pid, src))
            if src == dst:
                return {"src": self._aggregate_charge(src),
                        "dst": self._aggregate_charge(dst)}
            for ancestor in self._ancestors(dst):
                if self._in_subtree(src, ancestor):
                    continue
                if ancestor in self._disabled:
                    continue
                limit = self._limits.get(ancestor)
                if limit is not None and \
                        self._aggregate_charge(ancestor) + 1 > limit:
                    self._events[ancestor] = self._events.get(ancestor, 0) + 1
                    raise CgroupError(
                        "EAGAIN", "pids.max reached at %s" % ancestor)
            self._own_pids[src].discard(pid)
            self._own_pids.setdefault(dst, set()).add(pid)
            self._pid_cgroup[pid] = dst
            return {"src": self._aggregate_charge(src),
                    "dst": self._aggregate_charge(dst)}

    def exit_process(self, pid, zombie=True):
        """Exit a process; a zombie keeps its quota until reaped."""
        with self._lock:
            cgroup = self._pid_cgroup.get(pid)
            if cgroup is None:
                raise CgroupError("ENOENT", "unknown pid %s" % pid)
            if zombie:
                self._zombies.add(pid)
            else:
                self._own_pids[cgroup].discard(pid)
                del self._pid_cgroup[pid]
            return {"pid": pid, "zombie": zombie}

    def reap(self, pid):
        """Reap a zombie and release its quota."""
        with self._lock:
            if pid not in self._zombies:
                raise CgroupError("EINVAL", "pid %s is not a zombie" % pid)
            cgroup = self._pid_cgroup.pop(pid)
            self._own_pids[cgroup].discard(pid)
            self._zombies.discard(pid)
            return {"pid": pid, "pids": self._aggregate_charge(cgroup)}

    def events(self, cgroup):
        """Return {"max": int} for limit-hit events."""
        cgroup = self._norm(cgroup)
        with self._lock:
            return {"max": self._events.get(cgroup, 0)}

    def ack_events(self, cgroup):
        """Clear the event counter; returns {"max": 0}."""
        cgroup = self._norm(cgroup)
        with self._lock:
            self._events[cgroup] = 0
            return {"max": 0}

    def rename(self, cgroup, new_name):
        """Rename a cgroup while keeping its limit and processes."""
        cgroup = self._norm(cgroup)
        new_name = self._norm(new_name)
        with self._lock:
            if cgroup == new_name:
                return {"cgroup": new_name}

            def remapped(path):
                if path == cgroup:
                    return new_name
                if path.startswith(cgroup + "/"):
                    return new_name + path[len(cgroup):]
                return path

            for table in (self._limits, self._thread_mode, self._events,
                          self._own_threads):
                for path in [p for p in table if p != remapped(p)]:
                    table[remapped(path)] = table.pop(path)
            for path in [p for p in self._own_pids if p != remapped(p)]:
                for pid in self._own_pids.pop(path):
                    self._own_pids.setdefault(remapped(path), set()).add(pid)
                    self._pid_cgroup[pid] = remapped(path)
            self._disabled = {remapped(p) for p in self._disabled}
            return {"cgroup": new_name}

    def disable(self, cgroup):
        """Disable the controller for a cgroup."""
        cgroup = self._norm(cgroup)
        with self._lock:
            self._disabled.add(cgroup)
        return {"cgroup": cgroup, "enabled": False}

    def enable(self, cgroup):
        """Re-enable the controller without losing state."""
        cgroup = self._norm(cgroup)
        with self._lock:
            self._disabled.discard(cgroup)
        return {"cgroup": cgroup, "enabled": True}

    def resolve_path(self, namespace, path):
        """Resolve a cgroup-namespace path to the real hierarchy path."""
        path = self._norm(path)
        if not namespace:
            return path
        namespace = self._norm(namespace)
        if namespace == "/":
            return path
        return namespace + ("" if path == "/" else path)

    def dump_state(self):
        """Serialize limits, counts and thread modes."""
        with self._lock:
            return {
                "hierarchy": self.hierarchy,
                "limits": dict(self._limits),
                "thread_mode": dict(self._thread_mode),
                "events": dict(self._events),
                "disabled": sorted(self._disabled),
                "threads": dict(self._own_threads),
                "processes": [
                    {"pid": pid, "cgroup": cgroup,
                     "zombie": pid in self._zombies}
                    for pid, cgroup in sorted(self._pid_cgroup.items())
                ],
                "next_pid": self._next_pid,
                "next_tid": self._next_tid,
            }

    def load_state(self, state):
        """Restore every limit after a node restart."""
        with self._lock:
            self.hierarchy = state.get("hierarchy", self.hierarchy)
            self._limits = {self._norm(k): v
                            for k, v in state.get("limits", {}).items()}
            self._thread_mode = {self._norm(k): bool(v)
                                 for k, v in state.get("thread_mode", {}).items()}
            self._events = {self._norm(k): v
                            for k, v in state.get("events", {}).items()}
            self._disabled = {self._norm(p)
                              for p in state.get("disabled", [])}
            self._own_threads = {self._norm(k): v
                                 for k, v in state.get("threads", {}).items()}
            self._own_pids = {}
            self._pid_cgroup = {}
            self._zombies = set()
            for proc in state.get("processes", []):
                cgroup = self._norm(proc["cgroup"])
                pid = proc["pid"]
                self._own_pids.setdefault(cgroup, set()).add(pid)
                self._pid_cgroup[pid] = cgroup
                if proc.get("zombie"):
                    self._zombies.add(pid)
            self._next_pid = state.get(
                "next_pid",
                max(self._pid_cgroup, default=0) + 1)
            self._next_tid = state.get("next_tid", 1)
        return {"restored": len(self._pid_cgroup)}
