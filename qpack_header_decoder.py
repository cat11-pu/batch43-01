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
                 max_field_section_size=65536, compression_enabled=True):
        self.max_table_capacity = max_table_capacity
        self.max_blocked_streams = max_blocked_streams
        self.max_field_section_size = max_field_section_size
        self._capacity = max_table_capacity
        self._compression_enabled = compression_enabled
        self._table = []
        self._refs = []
        self._stream_refs = {}
        self._blocked_streams = set()
        self._closed = False
        self._goaway = False
        self._peer_reset_required = False

    def _raise_closed(self, code, detail=""):
        self._closed = True
        raise QpackError(code, detail)

    def _ensure_mutable(self):
        if self._closed:
            raise QpackError("QPACK_CONNECTION_CLOSED",
                             "connection already closed")
        if self._goaway:
            raise QpackError("QPACK_CONNECTION_CLOSED",
                             "GOAWAY received, new table work is forbidden")

    def negotiate_capacity(self, peer_capacity):
        """Apply the peer's dynamic-table capacity.

        A capacity above the local maximum raises
        QpackError("QPACK_ENCODER_STREAM_ERROR") and closes the connection.
        Otherwise returns {"capacity": int, "closed": False}.
        """
        if self._closed:
            raise QpackError("QPACK_CONNECTION_CLOSED",
                             "connection already closed")
        if (not isinstance(peer_capacity, int)
                or peer_capacity < 0
                or peer_capacity > self.max_table_capacity):
            self._raise_closed(
                "QPACK_ENCODER_STREAM_ERROR",
                "peer capacity %s exceeds local maximum %s"
                % (peer_capacity, self.max_table_capacity))
        self._capacity = peer_capacity
        return {"capacity": self._capacity, "closed": False}

    def capacity(self):
        """Return the currently negotiated capacity."""
        return self._capacity

    def is_closed(self):
        """Return True once the connection has been closed."""
        return self._closed

    def insert(self, name, value):
        """Insert one dynamic-table entry.

        Identical name/value pairs are deduplicated.  Inserting with capacity
        zero raises QpackError("QPACK_TABLE_CAPACITY_ZERO").  Returns
        {"inserted": bool, "duplicate": bool, "count": int}.
        """
        self._ensure_mutable()
        if not self._compression_enabled or self._capacity == 0:
            raise QpackError("QPACK_TABLE_CAPACITY_ZERO",
                             "dynamic table capacity is zero")
        for entry_name, entry_value in self._table:
            if entry_name == name and entry_value == value:
                return {"inserted": False, "duplicate": True,
                        "count": len(self._table)}
        self._table.append((name, value))
        self._refs.append(set())
        return {"inserted": True, "duplicate": False,
                "count": len(self._table)}

    def table_count(self):
        """Return the number of dynamic-table entries."""
        return len(self._table)

    def decode(self, block, stream_id):
        """Decode a header block of dynamic-table indexes.

        Returns a list of (name, value) tuples and records the references for
        ``stream_id``.  An unknown or already evicted index raises
        QpackError("QPACK_DECOMPRESSION_FAILED").
        """
        decoded = []
        referenced = self._stream_refs.setdefault(stream_id, set())
        for index in block:
            if (not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or index >= len(self._table)):
                raise QpackError("QPACK_DECOMPRESSION_FAILED",
                                 "unknown or evicted index %r" % (index,))
            self._refs[index].add(stream_id)
            referenced.add(index)
            decoded.append(self._table[index])
        return decoded

    def open_stream(self, stream_id):
        """Mark a stream blocked on the encoder.

        Exceeding ``max_blocked_streams`` raises
        QpackError("QPACK_DECOMPRESSION_FAILED").
        Returns {"blocked": int}.
        """
        self._ensure_mutable()
        if (stream_id not in self._blocked_streams
                and len(self._blocked_streams) >= self.max_blocked_streams):
            raise QpackError(
                "QPACK_DECOMPRESSION_FAILED",
                "blocked stream limit %s reached" % self.max_blocked_streams)
        self._blocked_streams.add(stream_id)
        return {"blocked": len(self._blocked_streams)}

    def release(self, stream_id):
        """Release all dynamic-table references held by a stream."""
        referenced = self._stream_refs.pop(stream_id, set())
        for index in referenced:
            if index < len(self._refs):
                self._refs[index].discard(stream_id)
        return {"released": stream_id}

    def reset_stream(self, stream_id):
        """Reset a stream and release its references."""
        self.release(stream_id)
        self._blocked_streams.discard(stream_id)
        return {"reset": stream_id}

    def evict(self):
        """Evict the oldest entry, which must be unreferenced.

        Referenced entries raise QpackError("QPACK_ENTRY_REFERENCED").
        Returns {"evicted": int}.
        """
        if not self._table:
            raise QpackError("QPACK_DECOMPRESSION_FAILED",
                             "no dynamic-table entry to evict")
        if self._refs[0]:
            raise QpackError("QPACK_ENTRY_REFERENCED",
                             "oldest entry still referenced by %s"
                             % sorted(self._refs[0]))
        self._table.pop(0)
        self._refs.pop(0)
        for referenced in self._stream_refs.values():
            shifted = set()
            for index in referenced:
                shifted.add(index - 1 if index > 0 else index)
            referenced.clear()
            referenced.update(shifted)
        return {"evicted": 1}

    def check_header_size(self, size):
        """Reject an oversized field section on the decode side.

        Raises QpackError("QPACK_HEADER_TOO_LARGE"); valid returns
        {"accepted": True}.
        """
        if size > self.max_field_section_size:
            raise QpackError(
                "QPACK_HEADER_TOO_LARGE",
                "field section %s exceeds limit %s"
                % (size, self.max_field_section_size))
        return {"accepted": True}

    def validate_huffman_padding(self, padding):
        """Validate Huffman padding: at most seven bits, all ones.

        Invalid padding raises QpackError("QPACK_HUFFMAN_ERROR").
        """
        if (not isinstance(padding, str)
                or len(padding) > 7
                or any(bit != "1" for bit in padding)):
            raise QpackError("QPACK_HUFFMAN_ERROR",
                             "illegal Huffman padding %r" % (padding,))
        return {"valid": True}

    def server_push_insert(self, name, value):
        """Server push must never mutate the client dynamic table.

        Raises QpackError("QPACK_PUSH_TABLE_FORBIDDEN").
        """
        raise QpackError("QPACK_PUSH_TABLE_FORBIDDEN",
                         "server push cannot mutate the dynamic table")

    def handle_goaway(self):
        """Mark the connection closed for new streams."""
        self._goaway = True
        return {"goaway": True}

    def peer_reset_required(self):
        """Return True when the peer must reset the dynamic table."""
        return self._peer_reset_required

    def dump_state(self):
        """Serialize capacity and table state."""
        return {
            "capacity": self._capacity,
            "max_table_capacity": self.max_table_capacity,
            "max_blocked_streams": self.max_blocked_streams,
            "max_field_section_size": self.max_field_section_size,
            "compression_enabled": self._compression_enabled,
            "table": list(self._table),
            "blocked_streams": list(self._blocked_streams),
        }

    def load_state(self, state):
        """Restore state after restart; capacity returns as zero."""
        self._capacity = 0
        self._table = []
        self._refs = []
        self._stream_refs = {}
        self._blocked_streams = set()
        self._closed = False
        self._goaway = False
        self._peer_reset_required = True
        limits = state or {}
        self.max_table_capacity = limits.get(
            "max_table_capacity", self.max_table_capacity)
        self.max_blocked_streams = limits.get(
            "max_blocked_streams", self.max_blocked_streams)
        self.max_field_section_size = limits.get(
            "max_field_section_size", self.max_field_section_size)
        self._compression_enabled = limits.get(
            "compression_enabled", self._compression_enabled)
        return {"capacity": 0, "peer_reset_required": True}

    def is_compression_enabled(self):
        """Return the state of the compression kill switch."""
        return self._compression_enabled

    def disable_compression(self):
        """Rollback switch: turn QPACK compression off for this connection.

        The dynamic table is drained, capacity is forced to zero and the peer
        is told to reset its table, so no indexed references can be accepted
        until capacity is renegotiated.
        """
        self._compression_enabled = False
        self._capacity = 0
        self._table = []
        self._refs = []
        self._stream_refs = {}
        self._blocked_streams.clear()
        self._peer_reset_required = True
        return {"compression_enabled": False,
                "peer_reset_required": True}
