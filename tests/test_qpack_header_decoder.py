import pytest

import qpack_header_decoder


def test_capacity_mismatch_closes_connection():
    decoder = qpack_header_decoder.QpackHeaderDecoder(max_table_capacity=4096)
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.negotiate_capacity(8192)
    assert exc.value.code == "QPACK_ENCODER_STREAM_ERROR"
    assert decoder.is_closed() is True


def test_negotiated_capacity_is_applied():
    decoder = qpack_header_decoder.QpackHeaderDecoder(max_table_capacity=4096)
    result = decoder.negotiate_capacity(512)
    assert result == {"capacity": 512, "closed": False}
    assert decoder.capacity() == 512


def test_blocked_streams_over_limit_fail():
    decoder = qpack_header_decoder.QpackHeaderDecoder(max_blocked_streams=2)
    decoder.open_stream(1)
    decoder.open_stream(2)
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.open_stream(3)
    assert exc.value.code == "QPACK_DECOMPRESSION_FAILED"


def test_duplicate_insert_is_deduplicated():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    first = decoder.insert("x-test", "one")
    second = decoder.insert("x-test", "one")
    assert first["inserted"] is True
    assert second["duplicate"] is True
    assert second["count"] == 1


def test_referenced_entry_cannot_be_evicted():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    decoder.insert("x-test", "one")
    assert decoder.decode([0], stream_id=1) == [("x-test", "one")]
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.evict()
    assert exc.value.code == "QPACK_ENTRY_REFERENCED"


def test_released_entry_can_be_evicted():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    decoder.insert("x-test", "one")
    decoder.decode([0], stream_id=1)
    decoder.release(1)
    assert decoder.evict() == {"evicted": 1}
    assert decoder.table_count() == 0


def test_unknown_index_fails_decompression():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.decode([5], stream_id=1)
    assert exc.value.code == "QPACK_DECOMPRESSION_FAILED"


def test_reset_stream_releases_references():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    decoder.insert("x-test", "one")
    decoder.decode([0], stream_id=7)
    decoder.reset_stream(7)
    assert decoder.evict() == {"evicted": 1}


def test_goaway_blocks_new_inserts():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    decoder.handle_goaway()
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.insert("x-test", "one")
    assert exc.value.code == "QPACK_CONNECTION_CLOSED"


def test_invalid_huffman_padding_is_rejected():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.validate_huffman_padding("0000")
    assert exc.value.code == "QPACK_HUFFMAN_ERROR"


def test_valid_huffman_padding_is_accepted():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    assert decoder.validate_huffman_padding("111") == {"valid": True}


def test_header_size_limit_is_enforced_on_decode():
    decoder = qpack_header_decoder.QpackHeaderDecoder(max_field_section_size=1024)
    assert decoder.check_header_size(1024)["accepted"] is True
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.check_header_size(1025)
    assert exc.value.code == "QPACK_HEADER_TOO_LARGE"


def test_zero_capacity_rejects_insert():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    decoder.negotiate_capacity(0)
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.insert("x-test", "one")
    assert exc.value.code == "QPACK_TABLE_CAPACITY_ZERO"


def test_server_push_cannot_touch_dynamic_table():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    with pytest.raises(qpack_header_decoder.QpackError) as exc:
        decoder.server_push_insert("x-test", "one")
    assert exc.value.code == "QPACK_PUSH_TABLE_FORBIDDEN"


def test_restart_resets_capacity_to_zero_and_requests_reset():
    decoder = qpack_header_decoder.QpackHeaderDecoder()
    decoder.negotiate_capacity(1024)
    decoder.insert("x-test", "one")
    state = decoder.dump_state()
    restarted = qpack_header_decoder.QpackHeaderDecoder()
    restarted.load_state(state)
    assert restarted.capacity() == 0
    assert restarted.table_count() == 0
    assert restarted.peer_reset_required() is True
