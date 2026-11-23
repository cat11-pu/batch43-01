"""IMA appraisal and measurement log.

Models the kernel IMA measurement/appraisal guarantees required by the
compliance review:

* every file lifecycle event (create/write/execute/delete/mmap/mount) is
  measured into one append-only log, including events from non-host
  namespaces;
* the log extends a virtual PCR in record order, so the aggregate value can
  always be reconciled against the ordered log and the boot firmware;
* appraisal failures deny execution (IMA_APPRAISAL_FAILED), files opened for
  writing cannot be executed (IMA_TOMTOU_DENIED) and invalid security.ima
  xattr signatures are rejected (IMA_XATTR_INVALID);
* the log never overwrites old records (overflow raises IMA_LOG_OVERFLOW),
  cannot be cleared, and survives restart via dump_state/load_state with the
  sequence continuing where it stopped;
* policy replacement is all-or-nothing: a malformed batch or a hash
  algorithm mismatch leaves the previously loaded policy intact.
"""

import copy
import hashlib
import json


class ImaError(Exception):
    """Error carrying a stable IMA error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ImaAppraisalLog:
    """Append-only integrity measurement log with appraisal decisions."""

    VALID_EVENTS = frozenset(
        ("create", "write", "execute", "delete", "mmap", "mount")
    )

    def __init__(self, hash_algo="sha256", log_size=100):
        self.hash_algo = hash_algo
        self.log_size = log_size
        self._entries = []
        self._next_sequence = 0
        self._pcr = self._zero_digest()
        self._write_open = set()
        self._generation = 0
        self._policy = {"hash_algo": hash_algo, "rules": []}

    def _zero_digest(self):
        return hashlib.new(self.hash_algo).hexdigest()

    def _extend_pcr(self, entry):
        digest = hashlib.new(self.hash_algo)
        digest.update(bytes.fromhex(self._pcr))
        digest.update(self._pcr_payload(entry))
        self._pcr = digest.hexdigest()
        return self._pcr

    @staticmethod
    def _pcr_payload(entry):
        canonical = {
            key: entry[key]
            for key in (
                "sequence",
                "file_id",
                "event",
                "namespace",
                "hash_algo",
                "content_hash",
            )
        }
        return json.dumps(
            canonical, sort_keys=True, separators=(",", ":")
        ).encode()

    def measure(self, file_id, content, event, namespace="host", atomic=True):
        """Append one measurement record.

        Valid events: create, write, execute, delete, mmap, mount.  A partial
        measurement raises "IMA_PARTIAL_MEASURE"; a full log raises
        "IMA_LOG_OVERFLOW" instead of overwriting.  Returns
        {"recorded": True, "sequence": int, "pcr": str}.
        """
        if not atomic:
            raise ImaError(
                "IMA_PARTIAL_MEASURE",
                "refusing to measure a partially written file %r" % file_id,
            )
        if event not in self.VALID_EVENTS:
            raise ImaError("IMA_INVALID_EVENT", "unknown event %r" % event)
        if len(self._entries) >= self.log_size:
            raise ImaError(
                "IMA_LOG_OVERFLOW",
                "append-only log is full; old records cannot be overwritten",
            )

        content_hash = hashlib.new(self.hash_algo, bytes(content)).hexdigest()
        entry = {
            "sequence": self._next_sequence,
            "file_id": file_id,
            "event": event,
            "namespace": namespace,
            "hash_algo": self.hash_algo,
            "content_hash": content_hash,
        }
        entry["pcr"] = self._extend_pcr(entry)
        self._entries.append(entry)
        self._next_sequence += 1
        return {
            "recorded": True,
            "sequence": entry["sequence"],
            "pcr": entry["pcr"],
        }

    def log_entries(self):
        """Return the append-only log entries in order."""
        return copy.deepcopy(self._entries)

    def appraise(self, file_id, trusted_hash, measured_hash):
        """Allow execution only for a matching hash.

        Mismatch raises ImaError("IMA_APPRAISAL_FAILED"); returns
        {"allowed": True} on success.
        """
        if trusted_hash != measured_hash:
            raise ImaError(
                "IMA_APPRAISAL_FAILED",
                "measured hash for %r does not match the trusted value"
                % file_id,
            )
        return {"allowed": True}

    def open_for_write(self, file_id):
        """Mark a file as opened for writing."""
        self._write_open.add(file_id)

    def execute(self, file_id):
        """Execute a file unless it was opened for write (TOCTOU).

        Raises ImaError("IMA_TOMTOU_DENIED").
        """
        if file_id in self._write_open:
            raise ImaError(
                "IMA_TOMTOU_DENIED",
                "file %r was opened for write before execute" % file_id,
            )
        return {"allowed": True}

    def verify_xattr(self, file_id, signature_valid):
        """Reject an invalid security.ima xattr signature.

        Raises ImaError("IMA_XATTR_INVALID").
        """
        if not signature_valid:
            raise ImaError(
                "IMA_XATTR_INVALID",
                "security.ima signature on %r is missing or invalid"
                % file_id,
            )
        return {"verified": True}

    def clear_log(self):
        """The measurement log is append-only; clear attempts fail."""
        raise ImaError(
            "IMA_LOG_APPEND_ONLY",
            "the measurement log cannot be cleared or overwritten",
        )

    def load_policy(self, policy):
        """Atomically replace the measurement policy.

        A hash algorithm mismatch raises "IMA_ALGO_MISMATCH"; a malformed
        batch raises "IMA_POLICY_INVALID" and leaves the old policy intact.
        Returns {"loaded": int, "generation": int}.
        """
        if (
            not isinstance(policy, dict)
            or "hash_algo" not in policy
            or "rules" not in policy
            or not isinstance(policy["rules"], list)
        ):
            raise ImaError(
                "IMA_POLICY_INVALID",
                "policy batch must be a mapping with 'hash_algo' and 'rules'",
            )
        if policy["hash_algo"] != self.hash_algo:
            raise ImaError(
                "IMA_ALGO_MISMATCH",
                "policy uses %r but the active log uses %r"
                % (policy["hash_algo"], self.hash_algo),
            )

        new_policy = {
            "hash_algo": policy["hash_algo"],
            "rules": copy.deepcopy(policy["rules"]),
        }
        self._policy = new_policy
        self._generation += 1
        return {
            "loaded": len(new_policy["rules"]),
            "generation": self._generation,
        }

    def policy_snapshot(self):
        """Return {"generation": int, "rules": int}."""
        return {
            "generation": self._generation,
            "rules": len(self._policy["rules"]),
        }

    def pcr_matches_log(self):
        """Return True when the PCR reflects the log order exactly."""
        pcr = self._zero_digest()
        for entry in self._entries:
            digest = hashlib.new(self.hash_algo)
            digest.update(bytes.fromhex(pcr))
            digest.update(self._pcr_payload(entry))
            pcr = digest.hexdigest()
        return pcr == self._pcr

    def dump_state(self):
        """Serialize the log, sequence and policy."""
        return {
            "hash_algo": self.hash_algo,
            "log_size": self.log_size,
            "entries": copy.deepcopy(self._entries),
            "next_sequence": self._next_sequence,
            "pcr": self._pcr,
            "write_open": sorted(self._write_open),
            "generation": self._generation,
            "policy": copy.deepcopy(self._policy),
        }

    def load_state(self, state):
        """Restore the log and continue its sequence after a restart."""
        required = (
            "hash_algo",
            "log_size",
            "entries",
            "next_sequence",
            "pcr",
            "write_open",
            "generation",
            "policy",
        )
        missing = [key for key in required if key not in state]
        if missing:
            raise ImaError(
                "IMA_STATE_INVALID",
                "restart state is missing: %s" % ", ".join(sorted(missing)),
            )

        self.hash_algo = state["hash_algo"]
        self.log_size = state["log_size"]
        self._entries = copy.deepcopy(state["entries"])
        self._next_sequence = state["next_sequence"]
        self._pcr = state["pcr"]
        self._write_open = set(state["write_open"])
        self._generation = state["generation"]
        self._policy = copy.deepcopy(state["policy"])
        return {"restored": len(self._entries)}
