"""MACsec MKA key agreement.

Implements key-server election, atomic SAK installation, per-SA replay
protection, integrity and cipher-suite enforcement and fail-closed state
restoration for a MACsec connectivity association.
"""

import threading


class MacsecError(Exception):
    """Error carrying a stable MACsec/MKA error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class MacsecKeyAgreement:
    """Key-server election, SAK installation and PN replay state."""

    def __init__(self, pn_limit=(1 << 32) - 1, participant_timeout=300):
        self.pn_limit = pn_limit
        self.participant_timeout = participant_timeout
        self._lock = threading.RLock()
        self._elected = None
        self._participant_count = 0
        self._cak = None
        self._channels = {}
        self._pn = {}
        self._exhausted_sas = set()
        self._material_owner = {}
        self._participants = {}
        self._mka_seq = {}

    def _require_election(self):
        if self._elected is None:
            raise MacsecError("MACSEC_ELECTION_INCOMPLETE")

    def elect_key_server(self, candidates):
        """Elect exactly one key server.

        ``candidates`` is a list of {"id", "priority", "sci"}; the lowest
        priority wins, ties break on the lowest SCI.  Repeating the election
        returns the same winner.  Empty input raises
        MacsecError("MACSEC_ELECTION_EMPTY").
        Returns {"elected": str, "participants": int}.
        """
        with self._lock:
            if not candidates:
                raise MacsecError("MACSEC_ELECTION_EMPTY")
            winner = min(
                candidates,
                key=lambda member: (member["priority"], member["sci"], member["id"]),
            )
            self._elected = winner["id"]
            self._participant_count = len(candidates)
            return {
                "elected": self._elected,
                "participants": self._participant_count,
            }

    def install_sak(self, sci, sak_id, key):
        """Install a SAK atomically after election completes.

        Before election raise MacsecError("MACSEC_ELECTION_INCOMPLETE").  A
        different active SAK raises MacsecError("MACSEC_SAK_CONFLICT"); only
        one SAK may be active per channel.  Same id is idempotent.
        """
        with self._lock:
            self._require_election()
            channel = self._channels.get(sci)
            if channel is not None:
                if channel["active_sak"] == sak_id:
                    if channel["key"] != key:
                        raise MacsecError("MACSEC_SAK_CONFLICT")
                    return {"active_sak": sak_id, "installed": True}
                raise MacsecError("MACSEC_SAK_CONFLICT")
            self._channels[sci] = {"active_sak": sak_id, "key": key}
            self._pn.setdefault(sak_id, 0)
            return {"active_sak": sak_id, "installed": True}

    def active_sak(self, sci):
        """Return the active SAK id for a channel, or None."""
        with self._lock:
            channel = self._channels.get(sci)
            return channel["active_sak"] if channel else None

    def rekey(self, sci, new_sak_id):
        """Switch a channel to a new SAK; returns {"active_sak": str}."""
        with self._lock:
            self._require_election()
            channel = self._channels.get(sci)
            if channel is None:
                raise MacsecError("MACSEC_CHANNEL_NOT_FOUND")
            old_sak_id = channel["active_sak"]
            if old_sak_id != new_sak_id:
                self._exhausted_sas.add(old_sak_id)
                channel["active_sak"] = new_sak_id
            self._pn.setdefault(new_sak_id, 0)
            return {"active_sak": new_sak_id}

    def rekey_required(self, sci):
        """Return True when the PN space is exhausted for a channel."""
        with self._lock:
            channel = self._channels.get(sci)
            if channel is None:
                return False
            return channel["active_sak"] in self._exhausted_sas

    def verify_icv(self, frame, icv_valid):
        """Drop a frame with a bad integrity check.

        Raise MacsecError("MKA_ICV_INVALID"); valid returns {"ok": True}.
        """
        if not icv_valid:
            raise MacsecError("MKA_ICV_INVALID")
        return {"ok": True}

    def check_pn(self, sa_id, pn):
        """Check the packet number for one security association.

        Counters are per-SA.  Return {"accepted": bool, "code": str|None};
        replay gives "MACSEC_REPLAY", exhaustion gives
        "MACSEC_SAK_EXHAUSTED" and must force a rekey while rejecting the old
        SAK.
        """
        with self._lock:
            if sa_id in self._exhausted_sas:
                return {"accepted": False, "code": "MACSEC_SAK_EXHAUSTED"}
            if sa_id not in self._pn:
                raise MacsecError("MACSEC_SA_UNKNOWN")
            if pn > self.pn_limit:
                self._exhausted_sas.add(sa_id)
                return {"accepted": False, "code": "MACSEC_SAK_EXHAUSTED"}
            if pn <= self._pn[sa_id]:
                return {"accepted": False, "code": "MACSEC_REPLAY"}
            self._pn[sa_id] = pn
            return {"accepted": True, "code": None}

    def negotiate_cipher(self, local_suite, peer_suite):
        """Return the agreed suite or fail without downgrade.

        Mismatch raises MacsecError("MACSEC_CIPHER_MISMATCH").
        """
        if local_suite != peer_suite:
            raise MacsecError("MACSEC_CIPHER_MISMATCH")
        return local_suite

    def bind_sak(self, ca_id, sak_id, key):
        """Bind SAK material to one connectivity association.

        Reusing the same key material under another CA raises
        MacsecError("MACSEC_SAK_REUSE").
        """
        with self._lock:
            owner = self._material_owner.get(key)
            if owner is not None and owner != ca_id:
                raise MacsecError("MACSEC_SAK_REUSE")
            self._material_owner[key] = ca_id
            return {"ca": ca_id, "sak": sak_id}

    def validate_offset(self, offset, frame_len):
        """Validate the confidentiality offset (4-byte aligned, inside frame).

        Raise MacsecError("MACSEC_OFFSET_INVALID") otherwise.
        """
        if (
            offset < 0
            or offset > frame_len
            or offset % 4 != 0
        ):
            raise MacsecError("MACSEC_OFFSET_INVALID")
        return {"ok": True}

    def set_cak(self, cak):
        """Set the connectivity association key used for state dumps."""
        with self._lock:
            self._cak = cak
            return {"ok": True}

    def add_participant(self, participant_id, now):
        """Add a live participant; returns {"count": int}."""
        with self._lock:
            self._participants[participant_id] = now
            return {"count": len(self._participants)}

    def timeout_participants(self, now):
        """Drop participants silent longer than the timeout.

        Returns {"removed": int}.
        """
        with self._lock:
            stale = [
                participant_id
                for participant_id, last_seen in self._participants.items()
                if now - last_seen > self.participant_timeout
            ]
            for participant_id in stale:
                del self._participants[participant_id]
            return {"removed": len(stale)}

    def participants(self):
        """Return the list of live participant ids."""
        with self._lock:
            return list(self._participants)

    def process_mka_frame(self, sci, seq):
        """Process an MKA frame once.

        A duplicate sequence number raises MacsecError("MACSEC_MKA_REPLAY").
        """
        with self._lock:
            last_seq = self._mka_seq.get(sci)
            if last_seq is not None and seq <= last_seq:
                raise MacsecError("MACSEC_MKA_REPLAY")
            self._mka_seq[sci] = seq
            return {"ok": True}

    def dump_state(self):
        """Serialize CAK, SAKs and per-SA packet numbers."""
        with self._lock:
            return {
                "cak": self._cak,
                "elected": self._elected,
                "saks": {
                    sci: channel["active_sak"]
                    for sci, channel in self._channels.items()
                },
                "pn": dict(self._pn),
                "exhausted_sas": list(self._exhausted_sas),
            }

    def load_state(self, state):
        """Restore state; a missing CAK fails closed.

        Raise MacsecError("MACSEC_STATE_INCOMPLETE") when the CAK is absent.
        """
        with self._lock:
            if not state or state.get("cak") is None:
                raise MacsecError("MACSEC_STATE_INCOMPLETE")
            self._cak = state["cak"]
            self._elected = state.get("elected")
            self._pn = dict(state.get("pn", {}))
            self._exhausted_sas = set(state.get("exhausted_sas", []))
            self._channels = {}
            for sci, sak_id in state.get("saks", {}).items():
                self._channels[sci] = {"active_sak": sak_id, "key": None}
            return {"ok": True}
