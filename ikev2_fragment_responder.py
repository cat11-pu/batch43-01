"""Responder-side IKEv2 state machine (skeleton)."""


class Ikev2Error(Exception):
    """Error with a stable protocol code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class Ikev2FragmentResponder:
    """IKEv2 responder that must survive fragmented IKE_AUTH and MOBIKE."""

    def __init__(self, supported_dh_groups=(14, 19, 20)):
        self.supported_dh_groups = tuple(supported_dh_groups)

    def receive_auth_fragment(self, spi_i, spi_r, frag_index, total_frags, payload):
        """Buffer one IKE_AUTH fragment.

        Returns {"status": "buffered"|"duplicate"|"complete", "received": int,
        "total": int, "payload": bytes|None}.  Raise
        Ikev2Error("IKE_FRAGMENT_INCOMPLETE") when the last index arrives while
        an earlier index is still missing.  Duplicates never increment count.
        """
        raise NotImplementedError

    def establish_sa(self, spi_i, spi_r, nonce, dh_group, cookie=None,
                     peer_address="0.0.0.0"):
        """Create or reuse an IKE SA for the exact (spi_i, spi_r) pair.

        Returns {"sa_id": int, "spi_r": str, "sa_count": int, "reused": bool}.
        A retransmission of the same pair must reuse the existing SA.  Raise
        Ikev2Error("IKE_DH_GROUP_UNSUPPORTED") for an unlisted DH group.
        """
        raise NotImplementedError

    def process_notify(self, spi_i, notify):
        """Handle an encrypted notify payload.

        A COOKIE challenge must be echoed: returns {"cookie_echo": str}.
        Raise Ikev2Error("IKE_COOKIE_REQUIRED") when the cookie value is absent.
        """
        raise NotImplementedError

    def detect_nat(self, spi_i):
        """Record NAT detection; returns {"nat_detected": bool}."""
        raise NotImplementedError

    def send_keepalive(self, spi_i):
        """Return {"destination_port": int}; NAT-T keepalives use 4500."""
        raise NotImplementedError

    def handle_mobike_update(self, spi_i, spi_r, new_address, authenticated):
        """Apply a MOBIKE address update only from an authenticated message.

        Raise Ikev2Error("IKE_MOBIKE_UNAUTH") and keep the old address when
        ``authenticated`` is false.  Returns {"peer_address": str}.
        """
        raise NotImplementedError

    def peer_address(self, spi_i):
        """Return the currently recorded peer address for an IKE SA."""
        raise NotImplementedError

    def process_ike_message(self, spi_i, message_id):
        """Windowed message-id check.

        Returns {"status": "ok"} for a first sighting, including out-of-order
        ids inside the window, and {"status": "replay"} for a duplicate.
        """
        raise NotImplementedError

    def add_child_sa(self, spi_i, child_id):
        """Attach a child SA to an IKE SA; returns {"child_id": str}."""
        raise NotImplementedError

    def rekey_child_sa(self, spi_i, new_key_id):
        """Atomically rotate the child SA to ``new_key_id``.

        Returns {"new_key_id": str, "old_key_id": str, "state": "switched"}.
        New packets must only use the new key afterwards.
        """
        raise NotImplementedError

    def decrypt_packet(self, spi_i, key_id):
        """Return {"accepted": bool, "key_id": str} for the given key."""
        raise NotImplementedError

    def drain_old_sa(self, spi_i):
        """Remove the old SA after drain; returns {"removed": bool,
        "remaining_sas": int}."""
        raise NotImplementedError

    def delete_ike_sa(self, spi_i):
        """Delete an IKE SA and all its child SAs.

        Returns {"deleted_child_sas": int, "residual_tunnels": int}.
        """
        raise NotImplementedError

    def lookup_sa(self, spi_i, spi_r, source_address):
        """Look up an SA by the full tuple, never by source address alone.

        Returns a dict with "sa_id" and "peer_address", or None.
        """
        raise NotImplementedError
