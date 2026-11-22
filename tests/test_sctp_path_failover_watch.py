import sctp_path_failover_watch


PEER = "10.0.0.1"


def _watch():
    return sctp_path_failover_watch.SctpPathFailoverWatch()


def _packet(src=PEER, vtag=0, chunks=None, checksum_valid=True, peer=None):
    packet = {
        "src": src,
        "dst": "10.0.0.2",
        "sport": 5000,
        "dport": 5000,
        "vtag": vtag,
        "checksum_valid": checksum_valid,
        "chunks": chunks or [],
    }
    if peer is not None:
        packet["peer"] = peer
    return packet


def _establish(watch, peer=PEER):
    watch.receive(_packet(src=peer, chunks=[{"type": "INIT", "vtag": 0}]))
    return watch.receive(_packet(src=peer, chunks=[{"type": "COOKIE_ECHO"}]))


def test_init_with_bad_verification_tag_aborts_without_association():
    watch = _watch()
    result = watch.receive(_packet(chunks=[{"type": "INIT", "vtag": 1234}]))
    assert result["action"] == "abort"
    assert result["code"] == "SCTP_BAD_VTAG"
    assert watch.associations() == []


def test_bad_checksum_packet_is_dropped():
    watch = _watch()
    result = watch.receive(
        _packet(chunks=[{"type": "INIT", "vtag": 0}], checksum_valid=False)
    )
    assert result["action"] == "drop"
    assert result["code"] == "SCTP_BAD_CHECKSUM"
    assert watch.associations() == []


def test_duplicate_cookie_echo_hits_existing_association():
    watch = _watch()
    first = _establish(watch)
    second = watch.receive(_packet(chunks=[{"type": "COOKIE_ECHO"}]))
    assert first["association"] == second["association"]
    assert len(watch.associations()) == 1


def test_unauthenticated_asconf_cannot_add_path():
    watch = _watch()
    _establish(watch)
    watch.add_path(PEER, "10.0.0.1", port=5000)
    result = watch.receive(
        _packet(
            chunks=[
                {
                    "type": "ASCONF",
                    "authenticated": False,
                    "add": [{"address": "10.0.0.9", "port": 5000}],
                }
            ]
        )
    )
    assert result["action"] == "drop"
    assert result["code"] == "SCTP_ASCONF_UNAUTH"
    assert len(watch.paths(PEER)) == 1


def test_partial_address_list_is_rejected_as_non_atomic():
    watch = _watch()
    _establish(watch)
    watch.add_path(PEER, "10.0.0.1", port=5000)
    result = watch.receive(
        _packet(
            chunks=[
                {
                    "type": "ASCONF",
                    "authenticated": True,
                    "atomic": False,
                    "add": [{"address": "10.0.0.9", "port": 5000}],
                }
            ]
        )
    )
    assert result["code"] == "SCTP_ASCONF_PARTIAL"
    assert len(watch.paths(PEER)) == 1


def test_sack_for_unsent_tsn_is_invalid():
    watch = _watch()
    _establish(watch)
    result = watch.receive(_packet(chunks=[{"type": "SACK", "cum_tsn": 5}]))
    assert result["action"] == "drop"
    assert result["code"] == "SCTP_SACK_INVALID"


def test_heartbeat_from_unverified_address_cannot_change_primary():
    watch = _watch()
    _establish(watch)
    watch.add_path(PEER, "10.0.0.1", port=5000, verified=True)
    watch.add_path(PEER, "10.0.0.9", port=5000, verified=False)
    watch.receive(
        _packet(
            src="10.0.0.9",
            peer=PEER,
            chunks=[{"type": "HEARTBEAT_ACK"}],
        )
    )
    assert watch.primary(PEER) == "10.0.0.1"


def test_same_address_different_port_are_distinct_paths():
    watch = _watch()
    watch.add_path(PEER, "10.0.0.1", port=5000)
    watch.add_path(PEER, "10.0.0.1", port=5001)
    assert len(watch.paths(PEER)) == 2


def test_restart_restores_send_sequence():
    watch = _watch()
    _establish(watch)
    watch.add_path(PEER, "10.0.0.1", port=5000)
    assert watch.send_seq(PEER, "10.0.0.1") == 0
    state = watch.checkpoint()
    restarted = sctp_path_failover_watch.SctpPathFailoverWatch()
    restarted.restore(state)
    assert restarted.send_seq(PEER, "10.0.0.1") == 1


def test_duplicate_data_stream_sequence_is_dropped():
    watch = _watch()
    _establish(watch)
    chunk = {"type": "DATA", "tsn": 10, "stream_seq": 1, "payload": b"abc"}
    assert watch.receive(_packet(chunks=[chunk]))["action"] == "accept"
    duplicate = watch.receive(_packet(chunks=[chunk]))
    assert duplicate["action"] == "drop"
    assert duplicate["code"] == "SCTP_DUP_DATA"


def test_stream_sequence_wrap_is_accepted_once():
    watch = _watch()
    _establish(watch)
    first = {"type": "DATA", "tsn": 20, "stream_seq": 65535, "payload": b"a"}
    second = {"type": "DATA", "tsn": 21, "stream_seq": 0, "payload": b"b"}
    assert watch.receive(_packet(chunks=[first]))["action"] == "accept"
    assert watch.receive(_packet(chunks=[second]))["action"] == "accept"


def test_failover_moves_primary_to_verified_backup():
    watch = _watch()
    _establish(watch)
    watch.add_path(PEER, "10.0.0.1", port=5000, verified=True)
    watch.add_path(PEER, "10.0.0.3", port=5000, verified=True)
    assert watch.primary(PEER) == "10.0.0.1"
    watch.failover(PEER)
    assert watch.primary(PEER) == "10.0.0.3"


def test_failover_does_not_create_second_association():
    watch = _watch()
    _establish(watch)
    watch.add_path(PEER, "10.0.0.1", port=5000)
    watch.add_path(PEER, "10.0.0.3", port=5000)
    watch.failover(PEER)
    assert len(watch.associations()) == 1


def test_chunk_padding_is_skipped_when_parsing():
    watch = _watch()
    _establish(watch)
    chunk = {"type": "DATA", "tsn": 30, "stream_seq": 2, "length": 17, "padding": 3, "payload": b"x"}
    result = watch.receive(_packet(chunks=[chunk]))
    assert result["action"] == "accept"
    assert result["payload"] == b"x"


def test_bad_checksum_does_not_change_existing_path_state():
    watch = _watch()
    _establish(watch)
    watch.add_path(PEER, "10.0.0.1", port=5000)
    watch.receive(_packet(chunks=[{"type": "HEARTBEAT_ACK"}], checksum_valid=False))
    assert watch.primary(PEER) == "10.0.0.1"
    assert len(watch.paths(PEER)) == 1
