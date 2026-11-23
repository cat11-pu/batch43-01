"""GTP-U tunnel admission and replay arbitration.

Every forwarding decision is made on the forwarding path against the
control-plane session table.  State (sessions, replay windows, per-tunnel
counters) is guarded by a reentrant lock so concurrent forwarder threads see
consistent accounting, and can be checkpointed so a process restart never
re-opens a replay window.
"""

import os
import tempfile
import threading


GTP_VERSION = 1


class GtpError(Exception):
    """Configuration or protocol failure raised by the arbiter."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class _Session:
    """One control-plane session bound to a local TEID."""

    def __init__(self, teid, ue_ip, source_ip, charging_id=None):
        self.teid = teid
        self.ue_ip = ue_ip
        self.source_ip = source_ip
        self.charging_id = charging_id
        self.packets = 0
        self.bytes = 0

    def to_dict(self):
        return {
            "teid": self.teid,
            "ue_ip": self.ue_ip,
            "source_ip": self.source_ip,
            "charging_id": self.charging_id,
            "packets": self.packets,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, data):
        session = cls(
            data["teid"],
            data["ue_ip"],
            data["source_ip"],
            charging_id=data.get("charging_id"),
        )
        session.packets = data.get("packets", 0)
        session.bytes = data.get("bytes", 0)
        return session


class GtpUTunnelArbiter:
    """Validates GTP-U packets against active sessions before forwarding."""

    def __init__(self, replay_window=8, state_path=None):
        self.replay_window = replay_window
        self.state_path = state_path
        self._lock = threading.RLock()
        self._sessions = {}
        self._charging_index = {}
        self._replay = {}
        self._stats = {
            "unknown_teid": 0,
            "unsolicited_source": 0,
            "bad_version": 0,
            "duplicate": 0,
            "ue_ip_mismatch": 0,
            "echo_allowed": 0,
            "echo_denied": 0,
            "delivered": 0,
        }

    def add_session(self, teid, ue_ip, source_ip, charging_id=None):
        """Register a control-plane session; reject any conflicting binding."""
        with self._lock:
            existing = self._sessions.get(teid)
            if existing is not None:
                same_binding = (
                    existing.ue_ip == ue_ip
                    and existing.source_ip == source_ip
                    and existing.charging_id == charging_id
                )
                if not same_binding:
                    raise GtpError(
                        "GTP_TEID_CONFLICT",
                        "TEID %s is already bound to another peer" % teid,
                    )
                return existing

            if charging_id is not None:
                owner = self._charging_index.get(charging_id)
                if owner is not None and owner != teid:
                    raise GtpError(
                        "GTP_CHARGING_ID_REUSED",
                        "charging id %r is already used by TEID %s"
                        % (charging_id, owner),
                    )

            session = _Session(teid, ue_ip, source_ip, charging_id)
            self._sessions[teid] = session
            if charging_id is not None:
                self._charging_index[charging_id] = teid
            self._replay.setdefault(teid, {"highest": None, "seen": set()})
            return session

    def _reject(self, stat_key, code):
        self._stats[stat_key] += 1
        return {
            "accepted": False,
            "code": code,
            "icmp": False,
            "forwarded": False,
        }

    def forward_uplink(
        self,
        teid,
        seq,
        src_ip=None,
        payload=b"",
        flow_label=None,
        fragment_index=0,
        version=1,
    ):
        """Admit an uplink packet, performing every check on the data path.

        A flow-label container never grants admission: the packet must still
        match a provisioned session.  Every fragment, not only the first, is
        validated against the session and negotiated source address.
        """
        with self._lock:
            session = self._sessions.get(teid)
            if session is None:
                return self._reject("unknown_teid", "GTP_UNKNOWN_TEID")

            if src_ip != session.source_ip:
                return self._reject(
                    "unsolicited_source", "GTP_UNSOLICITED_SOURCE"
                )

            if version != GTP_VERSION:
                return self._reject("bad_version", "GTP_BAD_VERSION")

            is_first_fragment = fragment_index <= 0
            if is_first_fragment and not self._check_replay(teid, seq):
                return self._reject("duplicate", "GTP_DUPLICATE")

            payload_len = len(payload)
            session.packets += 1
            session.bytes += payload_len
            self._stats["delivered"] += 1
            return {
                "accepted": True,
                "code": None,
                "icmp": False,
                "forwarded": True,
                "teid": teid,
                "fragment_index": fragment_index,
                "flow_label": flow_label,
            }

    def _check_replay(self, teid, seq):
        """Sliding-window replay check.

        Returns True exactly once per sequence inside the window.  Rejected
        packets never mutate the window, so a duplicate cannot push history
        out and make an older packet look fresh.
        """
        entry = self._replay.setdefault(
            teid, {"highest": None, "seen": set()}
        )
        highest = entry["highest"]
        window = self.replay_window

        if highest is None:
            entry["highest"] = seq
            entry["seen"] = {seq}
            return True

        floor = highest - window + 1
        if seq < floor:
            return False
        if seq in entry["seen"]:
            return False

        entry["seen"].add(seq)
        if seq > highest:
            entry["highest"] = seq
            floor = seq - window + 1
            entry["seen"] = {
                value for value in entry["seen"] if value >= floor
            }
        return True

    def forward_downlink(self, teid, dst_ip, payload=b""):
        """Admit a downlink packet; the UE IP must match the tunnel binding."""
        with self._lock:
            session = self._sessions.get(teid)
            if session is None:
                return self._reject("unknown_teid", "GTP_UNKNOWN_TEID")

            if dst_ip != session.ue_ip:
                return self._reject("ue_ip_mismatch", "GTP_UE_IP_MISMATCH")

            session.packets += 1
            session.bytes += len(payload)
            self._stats["delivered"] += 1
            return {
                "accepted": True,
                "code": None,
                "icmp": False,
                "forwarded": True,
                "teid": teid,
            }

    def handle_echo(self, src_ip, is_request=True):
        """Answer echo requests only from a negotiated peer.

        Unsolicited echo responses are never consumed, and requests from
        unknown sources are dropped so the node cannot be an amplifier.
        """
        with self._lock:
            known_peer = any(
                session.source_ip == src_ip
                for session in self._sessions.values()
            )
            allowed = bool(is_request) and known_peer
            if allowed:
                self._stats["echo_allowed"] += 1
            else:
                self._stats["echo_denied"] += 1
            return allowed

    def walk_extensions(self, headers):
        """Walk an extension-header chain using each header's own length.

        Each header carries the length of its own container, so the chain is
        advanced per header instead of assuming a fixed stride and reading
        fields from the neighbouring container.  A zero-length header ends the
        chain; malformed lengths raise a protocol error.
        """
        parsed = []
        for header in headers:
            if not isinstance(header, dict):
                raise GtpError(
                    "GTP_BAD_EXTENSION", "extension header must be a mapping"
                )
            length = header.get("length")
            if not isinstance(length, int) or length < 0:
                raise GtpError(
                    "GTP_BAD_EXTENSION", "extension header length is invalid"
                )
            if length == 0:
                break
            value = header.get("value", b"")
            if len(value) != length:
                raise GtpError(
                    "GTP_BAD_EXTENSION",
                    "extension header value does not match its length",
                )
            parsed.append(
                {
                    "type": header["type"],
                    "length": length,
                    "value": value,
                }
            )
        return parsed

    def checkpoint(self):
        """Snapshot sessions and replay windows, optionally persisting them."""
        with self._lock:
            state = {
                "replay_window": self.replay_window,
                "sessions": [
                    session.to_dict()
                    for session in self._sessions.values()
                ],
                "replay": {
                    str(teid): {
                        "highest": entry["highest"],
                        "seen": list(entry["seen"]),
                    }
                    for teid, entry in self._replay.items()
                },
            }

            if self.state_path:
                directory = os.path.dirname(os.path.abspath(self.state_path))
                fd, tmp_path = tempfile.mkstemp(
                    prefix=".gtp-checkpoint-", dir=directory
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        import json

                        json.dump(state, handle)
                    os.replace(tmp_path, self.state_path)
                except BaseException:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise

            return state

    def restore(self, state):
        """Rebuild sessions and replay windows from a checkpoint.

        ``state`` may be a checkpoint dict or a path to a persisted one.
        After restore, packets accepted before the checkpoint are still seen
        as duplicates and stay off the wire.
        """
        if isinstance(state, (str, bytes, os.PathLike)):
            import json

            with open(state, "r", encoding="utf-8") as handle:
                state = json.load(handle)

        with self._lock:
            self.replay_window = state.get(
                "replay_window", self.replay_window
            )
            self._sessions.clear()
            self._charging_index.clear()
            self._replay.clear()

            for data in state.get("sessions", []):
                session = _Session.from_dict(data)
                self._sessions[session.teid] = session
                if session.charging_id is not None:
                    self._charging_index[session.charging_id] = session.teid

            for teid_key, entry in state.get("replay", {}).items():
                self._replay[int(teid_key)] = {
                    "highest": entry.get(
                        "highest",
                        entry.get("base"),
                    ),
                    "seen": set(entry.get("seen", [])),
                }

            for teid in self._sessions:
                self._replay.setdefault(
                    teid, {"highest": None, "seen": set()}
                )

    def stats(self):
        with self._lock:
            result = dict(self._stats)
            result["tunnels"] = {
                teid: {
                    "packets": session.packets,
                    "bytes": session.bytes,
                }
                for teid, session in self._sessions.items()
            }
            return result
