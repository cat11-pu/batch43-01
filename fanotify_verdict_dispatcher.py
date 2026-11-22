"""fanotify verdict dispatcher with fail-closed queue handling (skeleton)."""


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
        raise NotImplementedError

    def close_handle(self, node_id, pid=100, pid_start=1):
        raise NotImplementedError

    def verdict(self, node_id, generation=1):
        raise NotImplementedError

    def overflow(self, count=1):
        raise NotImplementedError

    def rename(self, node_id, new_path, generation=2):
        raise NotImplementedError

    def restart(self):
        raise NotImplementedError

    def pending(self):
        raise NotImplementedError

    def mount_mark(self, node_id):
        raise NotImplementedError

    def scan_count(self, node_id):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
