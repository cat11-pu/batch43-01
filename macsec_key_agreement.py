"""MACsec MKA key agreement (skeleton)."""


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

    def elect_key_server(self, candidates):
        """Elect exactly one key server.

        ``candidates`` is a list of {"id", "priority", "sci"}; the lowest
        priority wins, ties break on the lowest SCI.  Repeating the election
        returns the same winner.  Empty input raises
        MacsecError("MACSEC_ELECTION_EMPTY").
        Returns {"elected": str, "participants": int}.
        """
        raise NotImplementedError

    def install_sak(self, sci, sak_id, key):
        """Install a SAK atomically after election completes.

        Before election raise MacsecError("MACSEC_ELECTION_INCOMPLETE").  A
        different active SAK raises MacsecError("MACSEC_SAK_CONFLICT"); only
        one SAK may be active per channel.  Same id is idempotent.
        """
        raise NotImplementedError

    def active_sak(self, sci):
        """Return the active SAK id for a channel, or None."""
        raise NotImplementedError

    def rekey(self, sci, new_sak_id):
        """Switch a channel to a new SAK; returns {"active_sak": str}."""
        raise NotImplementedError

    def rekey_required(self, sci):
        """Return True when the PN space is exhausted for a channel."""
        raise NotImplementedError

    def verify_icv(self, frame, icv_valid):
        """Drop a frame with a bad integrity check.

        Raise MacsecError("MKA_ICV_INVALID"); valid returns {"ok": True}.
        """
        raise NotImplementedError

    def check_pn(self, sa_id, pn):
        """Check the packet number for one security association.

        Counters are per-SA.  Return {"accepted": bool, "code": str|None};
        replay gives "MACSEC_REPLAY", exhaustion gives
        "MACSEC_SAK_EXHAUSTED" and must force a rekey while rejecting the old
        SAK.
        """
        raise NotImplementedError

    def negotiate_cipher(self, local_suite, peer_suite):
        """Return the agreed suite or fail without downgrade.

        Mismatch raises MacsecError("MACSEC_CIPHER_MISMATCH").
        """
        raise NotImplementedError

    def bind_sak(self, ca_id, sak_id, key):
        """Bind SAK material to one connectivity association.

        Reusing the same key material under another CA raises
        MacsecError("MACSEC_SAK_REUSE").
        """
        raise NotImplementedError

    def validate_offset(self, offset, frame_len):
        """Validate the confidentiality offset (4-byte aligned, inside frame).

        Raise MacsecError("MACSEC_OFFSET_INVALID") otherwise.
        """
        raise NotImplementedError

    def set_cak(self, cak):
        """Set the connectivity association key used for state dumps."""
        raise NotImplementedError

    def add_participant(self, participant_id, now):
        """Add a live participant; returns {"count": int}."""
        raise NotImplementedError

    def timeout_participants(self, now):
        """Drop participants silent longer than the timeout.

        Returns {"removed": int}.
        """
        raise NotImplementedError

    def participants(self):
        """Return the list of live participant ids."""
        raise NotImplementedError

    def process_mka_frame(self, sci, seq):
        """Process an MKA frame once.

        A duplicate sequence number raises MacsecError("MACSEC_MKA_REPLAY").
        """
        raise NotImplementedError

    def dump_state(self):
        """Serialize CAK, SAKs and per-SA packet numbers."""
        raise NotImplementedError

    def load_state(self, state):
        """Restore state; a missing CAK fails closed.

        Raise MacsecError("MACSEC_STATE_INCOMPLETE") when the CAK is absent.
        """
        raise NotImplementedError
