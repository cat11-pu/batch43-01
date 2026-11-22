"""SIP REGISTER nonce and binding server (skeleton)."""


class SipError(Exception):
    """Error carrying a stable SIP error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SipRegisterServer:
    """Nonce challenge and binding state for SIP REGISTER."""

    def __init__(self, nonce_ttl=300, clock_skew=900):
        self.nonce_ttl = nonce_ttl
        self.clock_skew = clock_skew

    def challenge(self, device_id, now=0):
        """Issue a fresh single-use nonce.

        Returns {"nonce": str, "issued_at": int}; every call returns a new
        nonce.
        """
        raise NotImplementedError

    def register(self, device_id, call_id, contacts, expires, cseq,
                 authorization, now=0, received=None, rport=None):
        """Process one REGISTER request.

        ``contacts`` is a list of {"uri": str, "q": float}.  ``authorization``
        is {"nonce": str, "response": str|None, "stale": bool}.

        Returns {"status": int, "bindings": list, "challenge": str|None,
        "primary_contact": str|None, "replay": bool}.

        Rules: a reused or too-old nonce, a missing response, or a stale flag
        returns 401 with a new challenge and leaves bindings untouched;
        expires=0 removes only the matching Contact; a replayed CSeq is not a
        new registration; NAT received/rport are stored for routing; the
        highest-q contact is primary.
        """
        raise NotImplementedError

    def bindings(self, device_id):
        """Return the current binding list for a device/AOR."""
        raise NotImplementedError

    def routing_target(self, device_id, call_id, contact):
        """Return the address replies must be sent to (NAT aware)."""
        raise NotImplementedError

    def disconnect(self, device_id, call_id):
        """Drop all bindings for a call on transport teardown.

        Returns {"removed": int}.
        """
        raise NotImplementedError

    def expire(self, now):
        """Remove bindings past their expires time.

        Returns {"expired": int}.
        """
        raise NotImplementedError
