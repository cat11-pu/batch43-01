"""IMA appraisal and measurement log (skeleton)."""


class ImaError(Exception):
    """Error carrying a stable IMA error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ImaAppraisalLog:
    """Append-only integrity measurement log with appraisal decisions."""

    def __init__(self, hash_algo="sha256", log_size=100):
        self.hash_algo = hash_algo
        self.log_size = log_size
        self._generation = 0

    def measure(self, file_id, content, event, namespace="host", atomic=True):
        """Append one measurement record.

        Valid events: create, write, execute, delete, mmap, mount.  A partial
        measurement raises "IMA_PARTIAL_MEASURE"; a full log raises
        "IMA_LOG_OVERFLOW" instead of overwriting.  Returns {"recorded": True,
        "sequence": int, "pcr": str}.
        """
        raise NotImplementedError

    def log_entries(self):
        """Return the append-only log entries in order."""
        raise NotImplementedError

    def appraise(self, file_id, trusted_hash, measured_hash):
        """Allow execution only for a matching hash.

        Mismatch raises ImaError("IMA_APPRAISAL_FAILED"); returns
        {"allowed": True} on success.
        """
        raise NotImplementedError

    def open_for_write(self, file_id):
        """Mark a file as opened for writing."""
        raise NotImplementedError

    def execute(self, file_id):
        """Execute a file unless it was opened for write (TOCTOU).

        Raises ImaError("IMA_TOMTOU_DENIED").
        """
        raise NotImplementedError

    def verify_xattr(self, file_id, signature_valid):
        """Reject an invalid security.ima xattr signature.

        Raises ImaError("IMA_XATTR_INVALID").
        """
        raise NotImplementedError

    def clear_log(self):
        """The measurement log is append-only; clear attempts fail."""
        raise NotImplementedError

    def load_policy(self, policy):
        """Atomically replace the measurement policy.

        A hash algorithm mismatch raises "IMA_ALGO_MISMATCH"; a malformed batch
        raises "IMA_POLICY_INVALID" and leaves the old policy intact.
        Returns {"loaded": int, "generation": int}.
        """
        raise NotImplementedError

    def policy_snapshot(self):
        """Return {"generation": int, "rules": int}."""
        raise NotImplementedError

    def pcr_matches_log(self):
        """Return True when the PCR reflects the log order exactly."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize the log, sequence and policy."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore the log and continue its sequence after a restart."""
        raise NotImplementedError
