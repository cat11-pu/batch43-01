"""MPLS label allocator and forwarding table."""

import heapq
import threading


class MplsError(Exception):
    """Error carrying a stable MPLS error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class MplsLabelAllocator:
    """Allocates labels per FEC and builds label stacks for LSPs."""

    RESERVED_MAX = 15

    def __init__(self, label_min=16, label_max=1048575, stack_limit=8):
        self.label_min = label_min
        self.label_max = label_max
        self.stack_limit = stack_limit
        self._lock = threading.Lock()
        self._fec_to_label = {}
        self._label_to_fec = {}
        self._released = []
        self._next = label_min
        self._mappings = {}
        self._paths = {}

    def allocate(self, fec_id):
        """Allocate a label for a FEC.

        Returns {"label": int, "reused": bool}.  Allocating the same FEC twice
        must return the same label; distinct FECs get distinct labels.  Raise
        MplsError("MPLS_LABEL_EXHAUSTED") when the pool is empty.
        """
        with self._lock:
            if fec_id in self._fec_to_label:
                return {"label": self._fec_to_label[fec_id], "reused": True}
            if self._released:
                label = heapq.heappop(self._released)
            elif self._next <= self.label_max:
                label = self._next
                self._next += 1
            else:
                raise MplsError("MPLS_LABEL_EXHAUSTED", "label pool is empty")
            self._fec_to_label[fec_id] = label
            self._label_to_fec[label] = fec_id
            return {"label": label, "reused": False}

    def assign_label(self, label, fec_id):
        """Bind an explicit label to a FEC.

        Labels 0-15 are reserved and raise MplsError("MPLS_LABEL_RESERVED").
        Returns {"label": int, "fec_id": str}.
        """
        with self._lock:
            if 0 <= label <= self.RESERVED_MAX:
                raise MplsError("MPLS_LABEL_RESERVED",
                                "label %d is reserved" % label)
            if label < self.label_min or label > self.label_max:
                raise MplsError("MPLS_LABEL_RESERVED",
                                "label %d out of range" % label)
            owner = self._label_to_fec.get(label)
            if owner is not None and owner != fec_id:
                raise MplsError("MPLS_LABEL_CONFLICT",
                                "label %d already bound to %s" % (label, owner))
            self._fec_to_label[fec_id] = label
            self._label_to_fec[label] = fec_id
            return {"label": label, "fec_id": fec_id}

    def release_fec(self, fec_id):
        """Release a FEC's label back to the pool.

        Returns {"released": bool, "label": int|None}; the label becomes
        reusable for later allocations.
        """
        with self._lock:
            label = self._fec_to_label.pop(fec_id, None)
            if label is None:
                return {"released": False, "label": None}
            self._label_to_fec.pop(label, None)
            heapq.heappush(self._released, label)
            return {"released": True, "label": label}

    def build_label_stack(self, fec_id, inner_labels=()):
        """Build the outgoing stack for a FEC.

        Returns {"labels": list, "depth": int}.  A stack deeper than
        ``stack_limit`` raises MplsError("MPLS_STACK_EXCEEDED"); the stack is
        never silently truncated.
        """
        outer = self.allocate(fec_id)["label"]
        labels = [outer] + list(inner_labels)
        if len(labels) > self.stack_limit:
            raise MplsError("MPLS_STACK_EXCEEDED",
                            "stack depth %d exceeds limit %d"
                            % (len(labels), self.stack_limit))
        return {"labels": labels, "depth": len(labels)}

    def pop(self, label_stack, ttl, experimental_bits=0):
        """Pop the outer label (PHP).

        Returns {"labels": list, "ttl": int, "experimental_bits": int}; TTL
        must decrement and the experimental bits must be preserved.
        """
        return {
            "labels": list(label_stack[1:]),
            "ttl": max(ttl - 1, 0),
            "experimental_bits": experimental_bits,
        }

    def install_mapping(self, label, next_hop):
        """Install an incoming-label to next-hop mapping.

        Reusing one label for a different next hop raises
        MplsError("MPLS_LABEL_CONFLICT").
        """
        with self._lock:
            existing = self._mappings.get(label)
            if existing is not None and existing != next_hop:
                raise MplsError("MPLS_LABEL_CONFLICT",
                                "label %s already maps to %s" % (label, existing))
            self._mappings[label] = next_hop
            return {"label": label, "next_hop": next_hop}

    def lookup_next_hop(self, label):
        """Return the next hop installed for a label, or None."""
        return self._mappings.get(label)

    def switch_to_backup(self, fec_id, backup_ready):
        """Switch a FEC to its backup path only when the backup is installed.

        Returns {"path": "primary"|"backup", "switched": bool}.  When
        ``backup_ready`` is false raise MplsError("MPLS_BACKUP_NOT_READY") and
        stay on the primary path.
        """
        with self._lock:
            if not backup_ready:
                self._paths.setdefault(fec_id, "primary")
                raise MplsError("MPLS_BACKUP_NOT_READY",
                                "backup path for %s not installed" % fec_id)
            self._paths[fec_id] = "backup"
            return {"path": "backup", "switched": True}

    def active_path(self, fec_id):
        """Return the current active path name for a FEC."""
        return self._paths.get(fec_id, "primary")

    def dump_state(self):
        """Serialize allocations and mappings."""
        with self._lock:
            return {
                "label_min": self.label_min,
                "label_max": self.label_max,
                "stack_limit": self.stack_limit,
                "fec_to_label": dict(self._fec_to_label),
                "mappings": dict(self._mappings),
                "paths": dict(self._paths),
            }

    def load_state(self, state):
        """Restore allocations and mappings after a restart."""
        with self._lock:
            self._fec_to_label = dict(state.get("fec_to_label", {}))
            self._label_to_fec = {
                label: fec for fec, label in self._fec_to_label.items()
            }
            self._mappings = dict(state.get("mappings", {}))
            self._paths = dict(state.get("paths", {}))
            in_use = set(self._label_to_fec)
            self._next = max([self.label_min] + [l + 1 for l in in_use])
            self._released = [
                l for l in range(self.label_min, self._next) if l not in in_use
            ]
            heapq.heapify(self._released)
