"""QPACK header decoder (skeleton)."""


class QpackError(Exception):
    """Error carrying a stable QPACK error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class QpackHeaderDecoder:
    """Decoder-side QPACK dynamic table with blocked-stream bookkeeping."""

    def __init__(self, max_table_capacity=4096, max_blocked_streams=16,
                 max_field_section_size=65536):
        self.max_table_capacity = max_table_capacity
        self.max_blocked_streams = max_blocked_streams
        self.max_field_section_size = max_field_section_size

    def negotiate_capacity(self, peer_capacity):
        """Apply the peer's dynamic-table capacity.

        A capacity above the local maximum raises
        QpackError("QPACK_ENCODER_STREAM_ERROR") and closes the connection.
        Otherwise returns {"capacity": int, "closed": False}.
        """
        raise NotImplementedError

    def capacity(self):
        """Return the currently negotiated capacity."""
        raise NotImplementedError

    def is_closed(self):
        """Return True once the connection has been closed."""
        raise NotImplementedError

    def insert(self, name, value):
        """Insert one dynamic-table entry.

        Identical name/value pairs are deduplicated.  Inserting with capacity
        zero raises QpackError("QPACK_TABLE_CAPACITY_ZERO").  Returns
        {"inserted": bool, "duplicate": bool, "count": int}.
        """
        raise NotImplementedError

    def table_count(self):
        """Return the number of dynamic-table entries."""
        raise NotImplementedError

    def decode(self, block, stream_id):
        """Decode a header block of dynamic-table indexes.

        Returns a list of (name, value) tuples and records the references for
        ``stream_id``.  An unknown or already evicted index raises
        QpackError("QPACK_DECOMPRESSION_FAILED").
        """
        raise NotImplementedError

    def open_stream(self, stream_id):
        """Mark a stream blocked on the encoder.

        Exceeding ``max_blocked_streams`` raises
        QpackError("QPACK_DECOMPRESSION_FAILED").
        Returns {"blocked": int}.
        """
        raise NotImplementedError

    def release(self, stream_id):
        """Release all dynamic-table references held by a stream."""
        raise NotImplementedError

    def reset_stream(self, stream_id):
        """Reset a stream and release its references."""
        raise NotImplementedError

    def evict(self):
        """Evict the oldest entry, which must be unreferenced.

        Referenced entries raise QpackError("QPACK_ENTRY_REFERENCED").
        Returns {"evicted": int}.
        """
        raise NotImplementedError

    def check_header_size(self, size):
        """Reject an oversized field section on the decode side.

        Raises QpackError("QPACK_HEADER_TOO_LARGE"); valid returns
        {"accepted": True}.
        """
        raise NotImplementedError

    def validate_huffman_padding(self, padding):
        """Validate Huffman padding: at most seven bits, all ones.

        Invalid padding raises QpackError("QPACK_HUFFMAN_ERROR").
        """
        raise NotImplementedError

    def server_push_insert(self, name, value):
        """Server push must never mutate the client dynamic table.

        Raises QpackError("QPACK_PUSH_TABLE_FORBIDDEN").
        """
        raise NotImplementedError

    def handle_goaway(self):
        """Mark the connection closed for new streams."""
        raise NotImplementedError

    def peer_reset_required(self):
        """Return True when the peer must reset the dynamic table."""
        raise NotImplementedError

    def dump_state(self):
        """Serialize capacity and table state."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore state after restart; capacity returns as zero."""
        raise NotImplementedError
