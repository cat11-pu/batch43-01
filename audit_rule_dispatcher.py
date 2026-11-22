"""Kernel audit rule dispatcher (skeleton)."""


class AuditError(Exception):
    """Error carrying a stable audit error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuditRuleDispatcher:
    """Queues audit events and evaluates ordered rules."""

    def __init__(self, queue_size=100, exclusions=()):
        self.queue_size = queue_size
        self._exclusions = list(exclusions)
        self._rules = []
        self._generation = 0
        self._disk_full = False
        self._running = True

    def enqueue(self, event):
        """Queue an audit event.

        A full queue fails closed for security events
        (AuditError("AUDIT_BACKLOG_OVERFLOW")) and records a drop code for
        others.  Disk-full raises AuditError("AUDIT_DISK_FULL") while the
        daemon stays running.  Returns {"queued": int, "code": str|None,
        "dropped": bool}.
        """
        raise NotImplementedError

    def log_entries(self):
        """Return the queued events in order."""
        raise NotImplementedError

    def add_rule(self, rule):
        """Add a rule; re-adding the same id is idempotent."""
        raise NotImplementedError

    def reload(self, rules):
        """Atomically replace all rules.

        Returns {"rules": int, "generation": int}; the old set must not linger.
        """
        raise NotImplementedError

    def dispatch(self, event):
        """Evaluate every rule without short-circuiting.

        A never-match rule must not hide a later always-record rule.  Returns
        {"matched": list[str], "recorded": bool}.
        """
        raise NotImplementedError

    def rename_watch(self, old_path, new_path):
        """Move a watched directory inode to its new path."""
        raise NotImplementedError

    def watch_paths(self):
        """Return the currently watched paths."""
        raise NotImplementedError

    def add_exclusion(self, path):
        """Add an exclusion path."""
        raise NotImplementedError

    def is_excluded(self, path):
        """Exclude a path and its children, but not a sibling prefix."""
        raise NotImplementedError

    def record_multipart(self, event_id, part, total):
        """Assemble multi-part audit records.

        Returns {"complete": bool, "parts": int}; the assembled record is
        written once when the last part arrives.
        """
        raise NotImplementedError

    def multipart_incomplete(self):
        """Return ids of multi-part events still missing parts."""
        raise NotImplementedError

    def normalize_event(self, event):
        """Normalize an event, recording auid="unset" for cron jobs."""
        raise NotImplementedError

    def classify_arch(self, arch):
        """Classify syscall architecture as b32 or b64, never drop b32."""
        raise NotImplementedError

    def mark_disk_full(self):
        """Mark the audit disk as full."""
        raise NotImplementedError

    def is_running(self):
        """Return True while the daemon has not been stopped."""
        raise NotImplementedError

    def rotate(self):
        """Flush pending events before discarding old files.

        Returns {"flushed": True, "lost": 0}.
        """
        raise NotImplementedError
