"""MPLS label allocator and forwarding table (skeleton)."""


class MplsError(Exception):
    """Error carrying a stable MPLS error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class MplsLabelAllocator:
    """Allocates labels per FEC and builds label stacks for LSPs."""

    def __init__(self, label_min=16, label_max=1048575, stack_limit=8):
        self.label_min = label_min
        self.label_max = label_max
        self.stack_limit = stack_limit

    def allocate(self, fec_id):
        """Allocate a label for a FEC.

        Returns {"label": int, "reused": bool}.  Allocating the same FEC twice
        must return the same label; distinct FECs get distinct labels.  Raise
        MplsError("MPLS_LABEL_EXHAUSTED") when the pool is empty.
        """
        raise NotImplementedError

    def assign_label(self, label, fec_id):
        """Bind an explicit label to a FEC.

        Labels 0-15 are reserved and raise MplsError("MPLS_LABEL_RESERVED").
        Returns {"label": int, "fec_id": str}.
        """
        raise NotImplementedError

    def release_fec(self, fec_id):
        """Release a FEC's label back to the pool.

        Returns {"released": bool, "label": int|None}; the label becomes
        reusable for later allocations.
        """
        raise NotImplementedError

    def build_label_stack(self, fec_id, inner_labels=()):
        """Build the outgoing stack for a FEC.

        Returns {"labels": list, "depth": int}.  A stack deeper than
        ``stack_limit`` raises MplsError("MPLS_STACK_EXCEEDED"); the stack is
        never silently truncated.
        """
        raise NotImplementedError

    def pop(self, label_stack, ttl, experimental_bits=0):
        """Pop the outer label (PHP).

        Returns {"labels": list, "ttl": int, "experimental_bits": int}; TTL
        must decrement and the experimental bits must be preserved.
        """
        raise NotImplementedError

    def install_mapping(self, label, next_hop):
        """Install an incoming-label to next-hop mapping.

        Reusing one label for a different next hop raises
        MplsError("MPLS_LABEL_CONFLICT").
        """
        raise NotImplementedError

    def lookup_next_hop(self, label):
        """Return the next hop installed for a label, or None."""
        raise NotImplementedError

    def switch_to_backup(self, fec_id, backup_ready):
        """Switch a FEC to its backup path only when the backup is installed.

        Returns {"path": "primary"|"backup", "switched": bool}.  When
        ``backup_ready`` is false raise MplsError("MPLS_BACKUP_NOT_READY") and
        stay on the primary path.
        """
        raise NotImplementedError

    def active_path(self, fec_id):
        """Return the current active path name for a FEC."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize allocations and mappings."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore allocations and mappings after a restart."""
        raise NotImplementedError
