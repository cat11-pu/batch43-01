"""Responder-side IKEv2 state machine.

The responder must tolerate fragmented IKE_AUTH exchanges, retransmitted
exchanges, MOBIKE updates and child-SA rekeys without leaking SAs or
accepting forged state changes.  All lookups are keyed on the IKE SPI pair,
never on a bare source address, so peers behind the same NAT cannot cross.
"""


class Ikev2Error(Exception):
    """Error with a stable protocol code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class Ikev2FragmentResponder:
    """IKEv2 responder that must survive fragmented IKE_AUTH and MOBIKE."""

    MESSAGE_WINDOW_SIZE = 64
    NAT_T_KEEPALIVE_PORT = 4500
    STANDARD_IKE_PORT = 500

    def __init__(self, supported_dh_groups=(14, 19, 20)):
        self.supported_dh_groups = tuple(supported_dh_groups)
        self._sa_by_spi_i = {}
        self._fragment_buffers = {}
        self._next_sa_id = 1

    def _get_sa(self, spi_i):
        sa = self._sa_by_spi_i.get(spi_i)
        if sa is None:
            raise Ikev2Error("IKE_SA_NOT_FOUND", "no SA for initiator SPI %r" % spi_i)
        return sa

    def _active_child(self, spi_i):
        sa = self._get_sa(spi_i)
        if not sa["child_sas"]:
            raise Ikev2Error("IKE_CHILD_SA_NOT_FOUND")
        child_id = next(reversed(sa["child_sas"]))
        return sa, sa["child_sas"][child_id]

    def receive_auth_fragment(self, spi_i, spi_r, frag_index, total_frags, payload):
        """Buffer one IKE_AUTH fragment.

        Returns {"status": "buffered"|"duplicate"|"complete", "received": int,
        "total": int, "payload": bytes|None}.  Raise
        Ikev2Error("IKE_FRAGMENT_INCOMPLETE") when the last index arrives while
        an earlier index is still missing.  Duplicates never increment count.
        """
        buffer = self._fragment_buffers.setdefault(
            spi_i, {"spi_r": spi_r, "total": total_frags, "frags": {}})

        if frag_index in buffer["frags"]:
            return {
                "status": "duplicate",
                "received": len(buffer["frags"]),
                "total": buffer["total"],
                "payload": None,
            }

        buffer["frags"][frag_index] = payload

        if len(buffer["frags"]) == buffer["total"]:
            assembled = b"".join(
                buffer["frags"][index] for index in range(buffer["total"]))
            del self._fragment_buffers[spi_i]
            return {
                "status": "complete",
                "received": buffer["total"],
                "total": buffer["total"],
                "payload": assembled,
            }

        if frag_index == buffer["total"] - 1:
            raise Ikev2Error(
                "IKE_FRAGMENT_INCOMPLETE",
                "fragment %d arrived but only %d of %d fragments are present"
                % (frag_index, len(buffer["frags"]), buffer["total"]))

        return {
            "status": "buffered",
            "received": len(buffer["frags"]),
            "total": buffer["total"],
            "payload": None,
        }

    def build_fragmented_response(self, spi_i, payload, fragment_size=128):
        """Split an IKE response (e.g. a long certificate chain).

        Every emitted piece, including a single-piece response, carries
        ``fragmented=True`` so the peer's reassembler recognises it.
        """
        if fragment_size <= 0:
            raise ValueError("fragment_size must be positive")
        pieces = [payload[offset:offset + fragment_size]
                  for offset in range(0, len(payload), fragment_size)]
        total = len(pieces)
        return [
            {
                "spi_i": spi_i,
                "fragmented": True,
                "frag_index": index,
                "total_frags": total,
                "payload": piece,
            }
            for index, piece in enumerate(pieces)
        ]

    def establish_sa(self, spi_i, spi_r, nonce, dh_group, cookie=None,
                     peer_address="0.0.0.0"):
        """Create or reuse an IKE SA for the exact (spi_i, spi_r) pair.

        Returns {"sa_id": int, "spi_r": str, "sa_count": int, "reused": bool}.
        A retransmission of the same pair must reuse the existing SA.  Raise
        Ikev2Error("IKE_DH_GROUP_UNSUPPORTED") for an unlisted DH group.
        """
        if dh_group not in self.supported_dh_groups:
            raise Ikev2Error(
                "IKE_DH_GROUP_UNSUPPORTED",
                "DH group %r is not in %r" % (dh_group, self.supported_dh_groups))

        existing = self._sa_by_spi_i.get(spi_i)
        if existing is not None and existing["spi_r"] == spi_r:
            return {
                "sa_id": existing["sa_id"],
                "spi_r": existing["spi_r"],
                "sa_count": len(self._sa_by_spi_i),
                "reused": True,
            }
        if existing is not None:
            raise Ikev2Error(
                "IKE_SPI_COLLISION",
                "initiator SPI %r already bound to responder SPI %r"
                % (spi_i, existing["spi_r"]))

        sa = {
            "sa_id": self._next_sa_id,
            "spi_i": spi_i,
            "spi_r": spi_r,
            "nonce": nonce,
            "dh_group": dh_group,
            "cookie": cookie,
            "peer_address": peer_address,
            "nat_detected": False,
            "seen_message_ids": set(),
            "child_sas": {},
        }
        self._next_sa_id += 1
        self._sa_by_spi_i[spi_i] = sa
        return {
            "sa_id": sa["sa_id"],
            "spi_r": spi_r,
            "sa_count": len(self._sa_by_spi_i),
            "reused": False,
        }

    def process_notify(self, spi_i, notify):
        """Handle an encrypted notify payload.

        A COOKIE challenge must be echoed: returns {"cookie_echo": str}.
        Raise Ikev2Error("IKE_COOKIE_REQUIRED") when the cookie value is absent.
        """
        sa = self._get_sa(spi_i)
        if notify.get("type") == "COOKIE":
            cookie = notify.get("cookie")
            if not cookie:
                raise Ikev2Error("IKE_COOKIE_REQUIRED")
            sa["cookie"] = cookie
            return {"cookie_echo": cookie}
        return {"handled": False}

    def detect_nat(self, spi_i):
        """Record NAT detection; returns {"nat_detected": bool}."""
        sa = self._get_sa(spi_i)
        sa["nat_detected"] = True
        return {"nat_detected": True}

    def send_keepalive(self, spi_i):
        """Return {"destination_port": int}; NAT-T keepalives use 4500."""
        sa = self._get_sa(spi_i)
        port = (self.NAT_T_KEEPALIVE_PORT if sa["nat_detected"]
                else self.STANDARD_IKE_PORT)
        return {"destination_port": port}

    def handle_mobike_update(self, spi_i, spi_r, new_address, authenticated):
        """Apply a MOBIKE address update only from an authenticated message.

        Raise Ikev2Error("IKE_MOBIKE_UNAUTH") and keep the old address when
        ``authenticated`` is false.  Returns {"peer_address": str}.
        """
        sa = self._get_sa(spi_i)
        if not authenticated:
            raise Ikev2Error(
                "IKE_MOBIKE_UNAUTH",
                "MOBIKE update for %r arrived without authentication" % new_address)
        if sa["spi_r"] != spi_r:
            raise Ikev2Error("IKE_SA_NOT_FOUND", "SPI pair does not match")
        sa["peer_address"] = new_address
        return {"peer_address": new_address}

    def peer_address(self, spi_i):
        """Return the currently recorded peer address for an IKE SA."""
        return self._get_sa(spi_i)["peer_address"]

    def process_ike_message(self, spi_i, message_id):
        """Windowed message-id check.

        Returns {"status": "ok"} for a first sighting, including out-of-order
        ids inside the window, and {"status": "replay"} for a duplicate.
        """
        sa = self._get_sa(spi_i)
        seen = sa["seen_message_ids"]
        if message_id in seen:
            return {"status": "replay"}

        seen.add(message_id)
        window_floor = max(seen) - self.MESSAGE_WINDOW_SIZE + 1
        if window_floor > 0:
            stale = [message_id for message_id in seen
                     if message_id < window_floor]
            for message_id in stale:
                seen.discard(message_id)
        return {"status": "ok"}

    def add_child_sa(self, spi_i, child_id):
        """Attach a child SA to an IKE SA; returns {"child_id": str}."""
        sa = self._get_sa(spi_i)
        sa["child_sas"][child_id] = {
            "child_id": child_id,
            "key_id": "key-init-%s" % child_id,
            "retired_key_id": None,
        }
        return {"child_id": child_id}

    def rekey_child_sa(self, spi_i, new_key_id):
        """Atomically rotate the child SA to ``new_key_id``.

        Returns {"new_key_id": str, "old_key_id": str, "state": "switched"}.
        New packets must only use the new key afterwards.
        """
        _sa, child = self._active_child(spi_i)
        old_key_id = child["key_id"]
        child["retired_key_id"] = old_key_id
        child["key_id"] = new_key_id
        return {
            "new_key_id": new_key_id,
            "old_key_id": old_key_id,
            "state": "switched",
        }

    def decrypt_packet(self, spi_i, key_id):
        """Return {"accepted": bool, "key_id": str} for the given key."""
        sa = self._get_sa(spi_i)
        accepted = any(child["key_id"] == key_id
                       for child in sa["child_sas"].values())
        return {"accepted": accepted, "key_id": key_id}

    def drain_old_sa(self, spi_i):
        """Remove the old SA after drain; returns {"removed": bool,
        "remaining_sas": int}."""
        sa = self._get_sa(spi_i)
        removed = 0
        for child in sa["child_sas"].values():
            if child["retired_key_id"] is not None:
                child["retired_key_id"] = None
                removed += 1
        remaining = sum(1 for child in sa["child_sas"].values()
                        if child["retired_key_id"] is not None)
        return {"removed": removed > 0, "remaining_sas": remaining}

    def delete_ike_sa(self, spi_i):
        """Delete an IKE SA and all its child SAs.

        Returns {"deleted_child_sas": int, "residual_tunnels": int}.
        """
        sa = self._sa_by_spi_i.pop(spi_i, None)
        if sa is None:
            return {"deleted_child_sas": 0, "residual_tunnels": 0}
        deleted_child_sas = len(sa["child_sas"])
        self._fragment_buffers.pop(spi_i, None)
        return {
            "deleted_child_sas": deleted_child_sas,
            "residual_tunnels": 0,
        }

    def lookup_sa(self, spi_i, spi_r, source_address):
        """Look up an SA by the full tuple, never by source address alone.

        Returns a dict with "sa_id" and "peer_address", or None.
        """
        sa = self._sa_by_spi_i.get(spi_i)
        if sa is None or sa["spi_r"] != spi_r:
            return None
        return {"sa_id": sa["sa_id"], "peer_address": sa["peer_address"]}
