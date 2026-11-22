"""cgroup pids controller (skeleton)."""


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

    def set_limit(self, cgroup, limit):
        """Set pids.max for a cgroup; returns {"cgroup": str, "limit": int}."""
        raise NotImplementedError

    def fork(self, cgroup):
        """Fork a process under a cgroup.

        At the limit raise CgroupError("EAGAIN").  Returns {"pid": int,
        "pids": int} with the aggregate count.
        """
        raise NotImplementedError

    def spawn_thread(self, cgroup):
        """Spawn a thread into a cgroup; enforces thread-mode limits."""
        raise NotImplementedError

    def pids(self, cgroup):
        """Return the aggregate process count including descendants."""
        raise NotImplementedError

    def tasks(self, cgroup):
        """Return the aggregate task count including threads."""
        raise NotImplementedError

    def set_thread_mode(self, cgroup, enabled):
        """Count threads against the limit when thread mode is on."""
        raise NotImplementedError

    def migrate(self, pid, src, dst):
        """Move a process atomically.

        Either cgroup is over its limit during the move raises
        CgroupError("EAGAIN"); there must be no intermediate double count.
        """
        raise NotImplementedError

    def exit_process(self, pid, zombie=True):
        """Exit a process; a zombie keeps its quota until reaped."""
        raise NotImplementedError

    def reap(self, pid):
        """Reap a zombie and release its quota."""
        raise NotImplementedError

    def events(self, cgroup):
        """Return {"max": int} for limit-hit events."""
        raise NotImplementedError

    def ack_events(self, cgroup):
        """Clear the event counter; returns {"max": 0}."""
        raise NotImplementedError

    def rename(self, cgroup, new_name):
        """Rename a cgroup while keeping its limit and processes."""
        raise NotImplementedError

    def disable(self, cgroup):
        """Disable the controller for a cgroup."""
        raise NotImplementedError

    def enable(self, cgroup):
        """Re-enable the controller without losing state."""
        raise NotImplementedError

    def resolve_path(self, namespace, path):
        """Resolve a cgroup-namespace path to the real hierarchy path."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize limits, counts and thread modes."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore every limit after a node restart."""
        raise NotImplementedError
