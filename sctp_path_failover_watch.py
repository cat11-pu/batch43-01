"""SCTP multi-homing association and path failover watch."""

import copy
import json
import os
import tempfile
import threading
from collections import OrderedDict


DEFAULT_PORT = 5000
DEFAULT_RX_PORT = 5000


class SctpError(Exception):
    """Association or path failure raised by the watch."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class _SequenceTracker:
    """Detects duplicates while allowing a finite-width sequence to wrap."""

    def __init__(self, bits):
        self.bits = bits
        self.modulus = 1 << bits
        self.half = self.modulus >> 1
        self.max_seq = None
        self.seen = set()

    def _candidate(self, value):
        raw = int(value) & (self.modulus - 1)
        if self.max_seq is None:
            return raw
        base = self.max_seq & (self.modulus - 1)
        candidate = self.max_seq + ((raw - base) & (self.modulus - 1))
        if candidate - self.max_seq > self.half:
            candidate -= self.modulus
        return candidate

    def observe(self, value):
        candidate = self._candidate(value)
        if candidate in self.seen:
            return False

        self.max_seq = candidate if self.max_seq is None else max(self.max_seq, candidate)
        self.seen.add(candidate)
        floor = self.max_seq - self.half
        self.seen = {seq for seq in self.seen if seq >= floor}
        return True

    def is_duplicate(self, value):
        return self._candidate(value) in self.seen

    def snapshot(self):
        return {"max_seq": self.max_seq, "seen": sorted(self.seen)}

    @classmethod
    def from_snapshot(cls, bits, state):
        tracker = cls(bits)
        if not isinstance(state, dict):
            return tracker
        max_seq = state.get("max_seq")
        tracker.max_seq = None if max_seq is None else int(max_seq)
        tracker.seen = {int(value) for value in state.get("seen", [])}
        return tracker


class SctpPathFailoverWatch:
    """Validates SCTP chunks and tracks multi-homed path state."""

    def __init__(self, state_path=None):
        self.state_path = state_path
        self._lock = threading.RLock()
        self._paths = OrderedDict()
        self._primary = {}
        self._primary_pinned = {}
        self._associations = OrderedDict()
        self._assoc_by_tuple = {}
        self._assoc_by_peer = {}
        self._pending = OrderedDict()
        self._next_assoc_id = 1
        if state_path and os.path.exists(state_path):
            self.restore()

    def add_path(self, peer, address, port=DEFAULT_PORT, verified=True, authenticated=True):
        if not authenticated:
            raise SctpError("SCTP_ASCONF_UNAUTH")
        key = self._path_key(address, port)
        with self._lock:
            table = self._paths.setdefault(peer, OrderedDict())
            entry = table.get(key)
            if entry is None:
                entry = {
                    "address": key[0],
                    "port": key[1],
                    "verified": bool(verified),
                    "authenticated": True,
                    "alive": bool(verified),
                    "fail_count": 0,
                    "last_seen": None,
                    "next_seq": 0,
                }
                table[key] = entry
            else:
                entry["verified"] = bool(verified)
                entry["authenticated"] = True
                if verified:
                    entry["alive"] = True
            self._select_primary_locked(peer)
            self._persist_locked()
            return self._path_snapshot(peer, key)

    def receive(self, packet):
        if not isinstance(packet, dict):
            return self._result("drop", "SCTP_PACKET_INVALID")
        if not self._checksum_valid(packet):
            return self._result("drop", "SCTP_BAD_CHECKSUM")

        chunks = self._chunks(packet)
        for chunk in chunks:
            if self._chunk_malformed(chunk):
                continue
            if self._chunk_type(chunk) == "INIT":
                if self._init_vtag(chunk, packet) != 0 or self._packet_vtag(packet) != 0:
                    return self._result("abort", "SCTP_BAD_VTAG")

        with self._lock:
            assoc = self._find_association_locked(packet)
            if assoc is not None:
                packet_vtag = self._packet_vtag(packet)
                if packet_vtag not in (0, assoc["vtag"]):
                    return self._result("drop", "SCTP_BAD_VTAG")

            accepted = []
            dropped = []
            payload_parts = []
            association_id = assoc["id"] if assoc is not None else None
            malformed = 0
            auth_seen = bool(packet.get("authenticated", packet.get("auth_verified", False)))
            mutated = False

            for chunk in chunks:
                if self._chunk_malformed(chunk):
                    malformed += 1
                    continue

                chunk_type = self._chunk_type(chunk)
                if not chunk_type:
                    malformed += 1
                    continue

                if chunk_type == "AUTH":
                    if self._chunk_authenticated(chunk, packet, auth_seen):
                        auth_seen = True
                        accepted.append(self._result("accept"))
                    else:
                        dropped.append("SCTP_ASCONF_UNAUTH")
                    continue

                result, changed = self._handle_chunk_locked(
                    packet, chunk, chunk_type, auth_seen
                )
                if changed:
                    mutated = True
                if result is None:
                    continue
                if result["action"] == "drop":
                    dropped.append(result["code"])
                    continue
                if result["action"] == "abort":
                    self._persist_if_mutated_locked(mutated)
                    return result

                accepted.append(result)
                if result.get("association") is not None:
                    association_id = result["association"]
                payload = result.get("payload")
                if payload:
                    payload_parts.append(payload)

            self._persist_if_mutated_locked(mutated)

            if accepted and payload_parts:
                response = self._result(
                    "accept",
                    association=association_id,
                    payload=b"".join(payload_parts),
                )
            elif accepted:
                action = "init"
                if not any(item.get("_init") for item in accepted):
                    action = "accept"
                response = self._result(action, association=association_id)
                if any(item.get("_init") for item in accepted):
                    init_result = next(item for item in accepted if item.get("_init"))
                    response["cookie"] = init_result.get("cookie")
                    response["vtag"] = init_result.get("vtag")
            elif dropped:
                response = self._result("drop", dropped[0])
            elif malformed:
                response = self._result("drop", "SCTP_CHUNK_MALFORMED")
            else:
                response = self._result("drop", "SCTP_CHUNK_UNKNOWN")

            if dropped:
                response["dropped"] = dropped
                response["drop_code"] = dropped[0]
            return response

    def failover(self, peer):
        with self._lock:
            table = self._paths.get(peer)
            if not table:
                raise SctpError("SCTP_NO_VERIFIED_PATH")

            verified = sorted(
                key for key, entry in table.items() if entry.get("verified")
            )
            if not verified:
                raise SctpError("SCTP_NO_VERIFIED_PATH")

            current = self._primary.get(peer)
            if current not in verified:
                target = verified[0]
            elif len(verified) == 1:
                target = current
            else:
                index = verified.index(current)
                target = verified[(index + 1) % len(verified)]

            if current in table:
                table[current]["alive"] = False
                table[current]["fail_count"] = int(table[current].get("fail_count", 0)) + 1
            table[target]["alive"] = True
            self._primary[peer] = target
            self._primary_pinned[peer] = True
            self._persist_locked()
            return target[0]

    def primary(self, peer):
        with self._lock:
            key = self._primary.get(peer)
            if key is None:
                return None
            return key[0]

    def paths(self, peer):
        with self._lock:
            table = self._paths.get(peer, {})
            return [self._path_snapshot(peer, key) for key in table]

    def send_seq(self, peer, path=None):
        with self._lock:
            assoc = self._association_for_peer_locked(peer)
            if assoc is None:
                raise SctpError("SCTP_NO_ASSOCIATION")
            table = self._paths.get(peer, {})
            key = self._resolve_path_locked(peer, path)
            entry = table[key]
            seq = int(entry.get("next_seq", 0))
            entry["next_seq"] = (seq + 1) & 0xFFFFFFFF
            assoc["sent_tsns"].add(seq)
            self._persist_locked()
            return seq

    def checkpoint(self):
        with self._lock:
            state = self._state_locked()
            self._write_state_locked(state)
            return copy.deepcopy(state)

    def restore(self, state=None):
        if state is None:
            if not self.state_path:
                raise SctpError("SCTP_STATE_INVALID")
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        elif isinstance(state, str):
            state = json.loads(state)

        if not isinstance(state, dict):
            raise SctpError("SCTP_STATE_INVALID")

        paths = OrderedDict()
        primary = {}
        primary_pinned = {}
        for peer, entries in state.get("paths", {}).items():
            table = OrderedDict()
            for item in entries:
                key = self._path_key(item.get("address"), item.get("port", DEFAULT_PORT))
                table[key] = {
                    "address": key[0],
                    "port": key[1],
                    "verified": bool(item.get("verified", True)),
                    "authenticated": bool(item.get("authenticated", True)),
                    "alive": bool(item.get("alive", item.get("verified", True))),
                    "fail_count": int(item.get("fail_count", 0)),
                    "last_seen": item.get("last_seen"),
                    "next_seq": int(item.get("next_seq", 0)) & 0xFFFFFFFF,
                }
            paths[peer] = table

        for peer, key in state.get("primary", {}).items():
            if key is not None:
                primary[peer] = self._path_key(key[0], key[1])
        for peer, pinned in state.get("primary_pinned", {}).items():
            primary_pinned[peer] = bool(pinned)

        associations = OrderedDict()
        assoc_by_tuple = {}
        assoc_by_peer = {}
        for item in state.get("associations", []):
            assoc_id = item.get("id")
            peer = item.get("peer")
            four_tuple = tuple(item.get("four_tuple", ()))
            assoc = {
                "id": assoc_id,
                "peer": peer,
                "four_tuple": four_tuple,
                "vtag": int(item.get("vtag", 0)),
                "sent_tsns": {int(value) for value in item.get("sent_tsns", [])},
                "received_tsns": _SequenceTracker.from_snapshot(
                    32, item.get("received_tsns", {})
                ),
                "streams": {},
            }
            for stream, tracker_state in item.get("streams", {}).items():
                assoc["streams"][int(stream)] = _SequenceTracker.from_snapshot(
                    16, tracker_state
                )
            associations[assoc_id] = assoc
            if four_tuple:
                assoc_by_tuple[four_tuple] = assoc_id
            if peer is not None:
                assoc_by_peer[peer] = assoc_id

        pending = OrderedDict()
        for item in state.get("pending", []):
            four_tuple = tuple(item.get("four_tuple", ()))
            pending[four_tuple] = {
                "peer": item.get("peer"),
                "four_tuple": four_tuple,
                "cookie": item.get("cookie"),
                "vtag": int(item.get("vtag", 0)),
            }

        next_assoc_id = int(state.get("next_assoc_id", len(associations) + 1))
        with self._lock:
            self._paths = paths
            self._primary = primary
            self._primary_pinned = primary_pinned
            self._associations = associations
            self._assoc_by_tuple = assoc_by_tuple
            self._assoc_by_peer = assoc_by_peer
            self._pending = pending
            self._next_assoc_id = max(next_assoc_id, len(associations) + 1)
            for peer, table in self._paths.items():
                self._select_primary_locked(peer)
            self._persist_locked()
            return self._state_locked()

    def associations(self):
        with self._lock:
            result = []
            for assoc in self._associations.values():
                peer = assoc["peer"]
                result.append(
                    {
                        "id": assoc["id"],
                        "peer": peer,
                        "four_tuple": list(assoc["four_tuple"]),
                        "vtag": assoc["vtag"],
                        "primary": self.primary(peer),
                        "paths": self.paths(peer),
                    }
                )
            return result

    def _handle_chunk_locked(self, packet, chunk, chunk_type, auth_seen):
        if chunk_type == "INIT":
            return self._handle_init_locked(packet, chunk)
        if chunk_type == "COOKIE_ECHO":
            return self._handle_cookie_echo_locked(packet, chunk)
        if chunk_type == "COOKIE_ACK":
            return self._result("accept"), False
        if chunk_type == "DATA":
            return self._handle_data_locked(packet, chunk)
        if chunk_type == "SACK":
            return self._handle_sack_locked(packet, chunk)
        if chunk_type in ("HEARTBEAT", "HEARTBEAT_ACK"):
            return self._handle_heartbeat_locked(packet, chunk)
        if chunk_type == "ASCONF":
            return self._handle_asconf_locked(packet, chunk, auth_seen)
        if chunk_type == "SHUTDOWN":
            return self._handle_shutdown_locked(packet), True
        if chunk_type == "ABORT":
            return self._handle_abort_locked(packet), True
        if chunk_type in ("SHUTDOWN_ACK", "SHUTDOWN_COMPLETE", "ERROR", "FORWARD_TSN"):
            return self._result("accept"), False
        return None, False

    def _handle_init_locked(self, packet, chunk):
        four_tuple = self._four_tuple(packet)
        existing_id = self._assoc_by_tuple.get(four_tuple)
        if existing_id is None:
            existing_id = self._assoc_by_peer.get(self._peer(packet))
        if existing_id is not None:
            return self._result("accept", association=existing_id), False

        pending = self._pending.get(four_tuple)
        if pending is None:
            vtag = (0x10000000 + self._next_assoc_id) & 0xFFFFFFFF
            pending = {
                "peer": self._peer(packet),
                "four_tuple": four_tuple,
                "cookie": "cookie-%d" % self._next_assoc_id,
                "vtag": vtag,
            }
            self._pending[four_tuple] = pending
            result = self._result("accept", association=None)
            result["_init"] = True
            result["cookie"] = pending["cookie"]
            result["vtag"] = pending["vtag"]
            return result, True

        result = self._result("accept", association=None)
        result["_init"] = True
        result["cookie"] = pending["cookie"]
        result["vtag"] = pending["vtag"]
        return result, False

    def _handle_cookie_echo_locked(self, packet, chunk):
        four_tuple = self._four_tuple(packet)
        peer = self._peer(packet)
        existing_id = self._assoc_by_tuple.get(four_tuple)
        if existing_id is None:
            existing_id = self._assoc_by_peer.get(peer)
        if existing_id is not None:
            self._pending.pop(four_tuple, None)
            return self._result("accept", association=existing_id), False

        pending = self._pending.get(four_tuple)
        if pending is None:
            for key, candidate in self._pending.items():
                if candidate.get("peer") == peer:
                    pending = candidate
                    four_tuple = key
                    break
        if pending is None:
            return self._result("drop", "SCTP_COOKIE_INVALID"), False
        packet_vtag = self._packet_vtag(packet)
        if packet_vtag not in (0, int(pending.get("vtag", 0))):
            return self._result("drop", "SCTP_BAD_VTAG"), False
        cookie = chunk.get("cookie")
        if cookie is not None and cookie != pending.get("cookie"):
            return self._result("drop", "SCTP_COOKIE_INVALID"), False

        assoc_id = "assoc-%d" % self._next_assoc_id
        self._next_assoc_id += 1
        assoc = {
            "id": assoc_id,
            "peer": peer,
            "four_tuple": four_tuple,
            "vtag": int(pending.get("vtag", 0)),
            "sent_tsns": set(),
            "received_tsns": _SequenceTracker(32),
            "streams": {},
        }
        self._associations[assoc_id] = assoc
        self._assoc_by_tuple[four_tuple] = assoc_id
        self._assoc_by_peer[peer] = assoc_id
        self._pending.pop(four_tuple, None)
        self._select_primary_locked(peer)
        return self._result("accept", association=assoc_id), True

    def _handle_data_locked(self, packet, chunk):
        assoc = self._find_association_locked(packet)
        if assoc is None:
            return self._result("drop", "SCTP_NO_ASSOCIATION"), False

        tsn = self._chunk_tsn(chunk)
        if tsn is None:
            return self._result("drop", "SCTP_CHUNK_MALFORMED"), False
        tsn = int(tsn) & 0xFFFFFFFF
        if assoc["received_tsns"].is_duplicate(tsn):
            return self._result("drop", "SCTP_DUP_DATA"), False

        stream = int(chunk.get("stream_id", chunk.get("stream", 0)))
        stream_seq = chunk.get("stream_seq", chunk.get("ssn"))
        tracker = None
        if stream_seq is not None:
            tracker = assoc["streams"].setdefault(stream, _SequenceTracker(16))
            if tracker.is_duplicate(int(stream_seq) & 0xFFFF):
                return self._result("drop", "SCTP_DUP_DATA"), False

        assoc["received_tsns"].observe(tsn)
        if tracker is not None:
            tracker.observe(int(stream_seq) & 0xFFFF)

        self._mark_path_seen_locked(packet)
        payload = chunk.get("payload", b"")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif payload is None:
            payload = b""
        elif not isinstance(payload, (bytes, bytearray)):
            payload = bytes(payload)
        response = self._result("accept", association=assoc["id"], payload=bytes(payload))
        response["tsn"] = tsn
        return response, True

    def _handle_sack_locked(self, packet, chunk):
        assoc = self._find_association_locked(packet)
        if assoc is None:
            return self._result("drop", "SCTP_NO_ASSOCIATION"), False

        cum_tsn = chunk.get(
            "cum_tsn",
            chunk.get("cumulative_tsn", chunk.get("cum_ack")),
        )
        if cum_tsn is None:
            return self._result("drop", "SCTP_SACK_INVALID"), False
        try:
            cum_tsn = int(cum_tsn) & 0xFFFFFFFF
        except (TypeError, ValueError):
            return self._result("drop", "SCTP_SACK_INVALID"), False

        sent = assoc["sent_tsns"]
        if cum_tsn not in sent:
            return self._result("drop", "SCTP_SACK_INVALID"), False

        gaps = chunk.get("gaps", chunk.get("gap_ack_blocks", [])) or []
        for gap in gaps:
            if isinstance(gap, dict):
                start = gap.get("start", gap.get("start_tsn"))
                end = gap.get("end", gap.get("end_tsn", start))
            else:
                try:
                    start, end = gap
                except (TypeError, ValueError):
                    return self._result("drop", "SCTP_SACK_INVALID"), False
            try:
                start = int(start) & 0xFFFFFFFF
                end = int(end) & 0xFFFFFFFF
            except (TypeError, ValueError):
                return self._result("drop", "SCTP_SACK_INVALID"), False
            if start > end:
                return self._result("drop", "SCTP_SACK_INVALID"), False
            if end - start + 1 > len(sent):
                return self._result("drop", "SCTP_SACK_INVALID"), False
            current = start
            while True:
                if current not in sent:
                    return self._result("drop", "SCTP_SACK_INVALID"), False
                if current == end:
                    break
                current += 1

        self._mark_path_seen_locked(packet)
        return self._result("accept", association=assoc["id"]), False

    def _handle_heartbeat_locked(self, packet, chunk):
        assoc = self._find_association_locked(packet)
        if assoc is None:
            return self._result("drop", "SCTP_NO_ASSOCIATION"), False

        key = self._path_key(
            packet.get("src"), packet.get("sport", DEFAULT_PORT)
        )
        table = self._paths.get(assoc["peer"], {})
        entry = table.get(key)
        if entry is None:
            return self._result("drop", "SCTP_PATH_UNKNOWN"), False
        if not entry.get("verified"):
            return self._result("drop", "SCTP_PATH_UNVERIFIED"), False

        entry["alive"] = True
        entry["last_seen"] = packet.get("now", chunk.get("now"))
        if self._primary.get(assoc["peer"]) is None:
            self._primary[assoc["peer"]] = key
        return self._result("accept", association=assoc["id"]), True

    def _handle_asconf_locked(self, packet, chunk, auth_seen):
        assoc = self._find_association_locked(packet)
        if assoc is None:
            return self._result("drop", "SCTP_NO_ASSOCIATION"), False
        if not self._chunk_authenticated(chunk, packet, auth_seen):
            return self._result("drop", "SCTP_ASCONF_UNAUTH"), False
        if chunk.get("atomic", True) is False:
            return self._result("drop", "SCTP_ASCONF_PARTIAL"), False

        adds, adds_valid = self._normalize_path_list(chunk.get("add", []))
        deletes, deletes_valid = self._normalize_path_list(
            chunk.get("delete", chunk.get("del", []))
        )
        if not adds_valid or not deletes_valid:
            return self._result("drop", "SCTP_ASCONF_PARTIAL"), False

        peer = assoc["peer"]
        table = self._paths.setdefault(peer, OrderedDict())
        for key in deletes:
            table.pop(key, None)
        for key, item in adds:
            entry = table.get(key)
            if entry is None:
                next_seq = item.get("next_seq", 0)
                entry = {
                    "address": key[0],
                    "port": key[1],
                    "verified": bool(item.get("verified", True)),
                    "authenticated": True,
                    "alive": bool(item.get("verified", True)),
                    "fail_count": 0,
                    "last_seen": None,
                    "next_seq": next_seq,
                }
                table[key] = entry
            else:
                entry["verified"] = bool(item.get("verified", entry["verified"]))
                entry["authenticated"] = True
        self._select_primary_locked(peer)
        return self._result("accept", association=assoc["id"]), True

    def _handle_shutdown_locked(self, packet):
        assoc = self._find_association_locked(packet)
        if assoc is None:
            return self._result("drop", "SCTP_NO_ASSOCIATION")
        self._remove_association_locked(assoc)
        return self._result("accept")

    def _handle_abort_locked(self, packet):
        assoc = self._find_association_locked(packet)
        if assoc is not None:
            self._remove_association_locked(assoc)
        return self._result("accept")

    def _remove_association_locked(self, assoc):
        self._associations.pop(assoc["id"], None)
        self._assoc_by_tuple.pop(assoc["four_tuple"], None)
        if self._assoc_by_peer.get(assoc["peer"]) == assoc["id"]:
            self._assoc_by_peer.pop(assoc["peer"], None)

    def _find_association_locked(self, packet):
        four_tuple = self._four_tuple(packet)
        assoc_id = self._assoc_by_tuple.get(four_tuple)
        if assoc_id is None:
            assoc_id = self._assoc_by_peer.get(self._peer(packet))
        if assoc_id is None:
            return None
        return self._associations.get(assoc_id)

    def _association_for_peer_locked(self, peer):
        assoc_id = self._assoc_by_peer.get(peer)
        if assoc_id is None:
            return None
        return self._associations.get(assoc_id)

    def _select_primary_locked(self, peer):
        table = self._paths.get(peer, {})
        verified = sorted(key for key, entry in table.items() if entry.get("verified"))
        current = self._primary.get(peer)
        if not verified:
            if current not in table:
                self._primary.pop(peer, None)
            return
        if current not in verified:
            self._primary[peer] = verified[0]
        elif not self._primary_pinned.get(peer) and verified[0] < current:
            self._primary[peer] = verified[0]

    def _resolve_path_locked(self, peer, selector):
        table = self._paths.get(peer, {})
        if not table:
            raise SctpError("SCTP_PATH_UNKNOWN")
        if selector is None:
            key = self._primary.get(peer)
            if key not in table:
                raise SctpError("SCTP_PATH_UNKNOWN")
            return key
        if isinstance(selector, dict):
            key = self._path_key(
                selector.get("address"),
                selector.get("port", DEFAULT_PORT),
            )
            if key in table:
                return key
            raise SctpError("SCTP_PATH_UNKNOWN")
        if isinstance(selector, (tuple, list)) and len(selector) == 2:
            key = self._path_key(selector[0], selector[1])
            if key in table:
                return key
            raise SctpError("SCTP_PATH_UNKNOWN")

        address = str(selector)
        matches = [key for key in table if key[0] == address]
        if len(matches) == 1:
            return matches[0]
        primary = self._primary.get(peer)
        if primary in matches:
            return primary
        if matches:
            return sorted(matches)[0]
        raise SctpError("SCTP_PATH_UNKNOWN")

    def _triple_tuple(self, packet):
        return self._four_tuple(packet)

    def _four_tuple(self, packet):
        return (
            str(packet.get("src", "")),
            str(packet.get("dst", "")),
            self._port(packet.get("sport", DEFAULT_PORT)),
            self._port(packet.get("dport", DEFAULT_RX_PORT)),
        )

    @staticmethod
    def _peer(packet):
        peer = packet.get("peer")
        if peer is None:
            peer = packet.get("src")
        return peer

    @staticmethod
    def _path_key(address, port):
        if address is None or str(address) == "":
            raise SctpError("SCTP_PATH_INVALID")
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise SctpError("SCTP_PATH_INVALID")
        if not 1 <= port <= 65535:
            raise SctpError("SCTP_PATH_INVALID")
        return (str(address), port)

    @staticmethod
    def _port(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _normalize_path_list(self, values):
        if values is None:
            return [], True
        if isinstance(values, dict):
            values = [values]
        elif isinstance(values, str):
            values = [values]
        elif (
            isinstance(values, tuple)
            and len(values) == 2
            and isinstance(values[1], int)
        ):
            values = [values]
        try:
            values = list(values)
        except TypeError:
            return [], False

        normalized = []
        for item in values:
            if isinstance(item, str):
                item = {"address": item}
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                item = {"address": item[0], "port": item[1]}
            if not isinstance(item, dict):
                return [], False
            try:
                key = self._path_key(
                    item.get("address"),
                    item.get("port", DEFAULT_PORT),
                )
                next_seq = int(item.get("next_seq", 0)) & 0xFFFFFFFF
            except SctpError:
                return [], False
            except (TypeError, ValueError):
                return [], False
            normalized_item = copy.deepcopy(item)
            normalized_item["next_seq"] = next_seq
            normalized.append((key, normalized_item))
        return normalized, True

    @staticmethod
    def _packet_vtag(packet):
        try:
            value = packet.get("vtag", packet.get("verification_tag", 0))
            return int(value) & 0xFFFFFFFF
        except (TypeError, ValueError):
            return -1

    def _init_vtag(self, chunk, packet):
        if "vtag" in chunk:
            value = chunk["vtag"]
        elif "verification_tag" in chunk:
            value = chunk["verification_tag"]
        elif "init_vtag" in chunk:
            value = chunk["init_vtag"]
        else:
            value = packet.get("vtag", 0)
        try:
            return int(value) & 0xFFFFFFFF
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _checksum_valid(packet):
        if packet.get("checksum_valid") is False:
            return False
        if packet.get("checksum_ok") is False:
            return False
        checksum = packet.get("checksum")
        if checksum is False or checksum in ("bad", "invalid"):
            return False
        return True

    @staticmethod
    def _chunks(packet):
        chunks = packet.get("chunks", [])
        if chunks is None:
            return []
        if isinstance(chunks, (bytes, bytearray)):
            return SctpPathFailoverWatch._parse_raw_chunks(chunks)
        if isinstance(chunks, dict):
            return [chunks]
        try:
            return list(chunks)
        except TypeError:
            return []

    @staticmethod
    def _parse_raw_chunks(data):
        chunks = []
        offset = 0
        total = len(data)
        while offset + 4 <= total:
            chunk_type = data[offset]
            length = int.from_bytes(data[offset + 2 : offset + 4], "big")
            if length < 4 or offset + length > total:
                chunks.append({"type": "MALFORMED", "malformed": True})
                break
            body = bytes(data[offset + 4 : offset + length])
            chunks.append(
                {
                    "type": SctpPathFailoverWatch._raw_chunk_name(chunk_type),
                    "raw": body,
                    "length": length,
                    "padding": (-length) % 4,
                }
            )
            offset += (length + 3) & ~3
        return chunks

    @staticmethod
    def _raw_chunk_name(chunk_type):
        names = {
            0: "DATA",
            1: "INIT",
            2: "INIT_ACK",
            3: "COOKIE_ECHO",
            4: "COOKIE_ACK",
            5: "SHUTDOWN",
            6: "SHUTDOWN_ACK",
            7: "ERROR",
            8: "HEARTBEAT",
            9: "HEARTBEAT_ACK",
            10: "ABORT",
            11: "SHUTDOWN_COMPLETE",
            15: "AUTH",
            0xC0: "ASCONF",
            0xC1: "ASCONF_ACK",
        }
        return names.get(chunk_type, "UNKNOWN")

    @staticmethod
    def _chunk_malformed(chunk):
        if not isinstance(chunk, dict):
            return True
        if chunk.get("malformed") or chunk.get("corrupt"):
            return True
        if chunk.get("valid") is False:
            return True
        return False

    @staticmethod
    def _chunk_type(chunk):
        if not isinstance(chunk, dict):
            return ""
        chunk_type = chunk.get("type", "")
        if not isinstance(chunk_type, str):
            return ""
        return chunk_type.strip().upper()

    @staticmethod
    def _chunk_tsn(chunk):
        value = chunk.get("tsn", chunk.get("TSN"))
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _chunk_authenticated(chunk, packet, auth_seen):
        if "authenticated" in chunk:
            return bool(chunk["authenticated"])
        if "auth_verified" in chunk:
            return bool(chunk["auth_verified"])
        if "auth_valid" in chunk:
            return bool(chunk["auth_valid"])
        if "hmac_valid" in chunk:
            return bool(chunk["hmac_valid"])
        if auth_seen:
            return True
        if "authenticated" in packet:
            return bool(packet["authenticated"])
        if "auth_verified" in packet:
            return bool(packet["auth_verified"])
        if "auth_valid" in packet:
            return bool(packet["auth_valid"])
        if "hmac_valid" in packet:
            return bool(packet["hmac_valid"])
        return False

    def _mark_path_seen_locked(self, packet):
        peer = self._peer(packet)
        if peer is None:
            return
        try:
            key = self._path_key(packet.get("src"), packet.get("sport", DEFAULT_PORT))
        except SctpError:
            return
        entry = self._paths.get(peer, {}).get(key)
        if entry is not None:
            entry["last_seen"] = packet.get("now")
            entry["alive"] = True

    def _path_snapshot(self, peer, key):
        entry = self._paths.get(peer, {}).get(key)
        if entry is None:
            return None
        snapshot = copy.deepcopy(entry)
        snapshot["primary"] = self._primary.get(peer) == key
        return snapshot

    @staticmethod
    def _result(action, code=None, association=None, payload=None):
        result = {"action": action, "code": code, "association": association}
        if payload is not None:
            result["payload"] = payload
        return result

    def _state_locked(self):
        paths = OrderedDict()
        for peer, table in self._paths.items():
            paths[peer] = [copy.deepcopy(entry) for entry in table.values()]

        associations = []
        for assoc in self._associations.values():
            associations.append(
                {
                    "id": assoc["id"],
                    "peer": assoc["peer"],
                    "four_tuple": list(assoc["four_tuple"]),
                    "vtag": assoc["vtag"],
                    "sent_tsns": sorted(assoc["sent_tsns"]),
                    "received_tsns": assoc["received_tsns"].snapshot(),
                    "streams": {
                        str(stream): tracker.snapshot()
                        for stream, tracker in assoc["streams"].items()
                    },
                }
            )

        return {
            "version": 1,
            "paths": paths,
            "primary": {
                peer: (list(key) if key is not None else None)
                for peer, key in self._primary.items()
            },
            "primary_pinned": dict(self._primary_pinned),
            "associations": associations,
            "pending": [copy.deepcopy(item) for item in self._pending.values()],
            "next_assoc_id": self._next_assoc_id,
        }

    def _persist_if_mutated_locked(self, mutated):
        if mutated:
            self._persist_locked()

    def _persist_locked(self):
        if not self.state_path:
            return
        self._write_state_locked(self._state_locked())

    def _write_state_locked(self, state):
        if not self.state_path:
            return
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".sctp-state-", suffix=".json", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
