"""SIP REGISTER nonce and binding server."""

import secrets
import threading


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
        self._lock = threading.RLock()
        self._nonces = {}
        self._bindings = {}
        self._last_cseq = {}

    def challenge(self, device_id, now=0):
        """Issue a fresh single-use nonce.

        Returns {"nonce": str, "issued_at": int}; every call returns a new
        nonce.
        """
        nonce = secrets.token_hex(16)
        with self._lock:
            self._nonces[nonce] = {
                "device_id": device_id,
                "issued_at": now,
                "used": False,
            }
        return {"nonce": nonce, "issued_at": now}

    def _validate_nonce(self, device_id, authorization, now):
        """Return an error string, or None when the auth header is valid.

        A well-formed, in-window nonce is single-use and consumed here.
        """
        if not authorization:
            return "missing-authorization"
        nonce = authorization.get("nonce")
        record = self._nonces.get(nonce) if nonce else None
        if record is None or record["device_id"] != device_id:
            return "unknown-nonce"
        if record["used"]:
            return "nonce-reused"
        if now - record["issued_at"] > self.nonce_ttl + self.clock_skew:
            return "nonce-expired"
        record["used"] = True
        if authorization.get("stale"):
            return "stale"
        if authorization.get("response") is None:
            return "missing-response"
        return None

    def _binding_list(self, device_id):
        return [dict(b) for b in self._bindings.get(device_id, [])]

    def _primary(self, device_id):
        bindings = self._bindings.get(device_id, [])
        if not bindings:
            return None
        return max(bindings, key=lambda b: b["q"])["contact"]

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
        with self._lock:
            error = self._validate_nonce(device_id, authorization, now)
            if error is not None:
                return {
                    "status": 401,
                    "bindings": self._binding_list(device_id),
                    "challenge": self.challenge(device_id, now)["nonce"],
                    "primary_contact": self._primary(device_id),
                    "replay": False,
                }

            cseq_key = (device_id, call_id)
            last_cseq = self._last_cseq.get(cseq_key)
            if last_cseq is not None and cseq <= last_cseq:
                return {
                    "status": 200,
                    "bindings": self._binding_list(device_id),
                    "challenge": None,
                    "primary_contact": self._primary(device_id),
                    "replay": True,
                }
            self._last_cseq[cseq_key] = cseq

            bindings = self._bindings.setdefault(device_id, [])
            if expires == 0:
                remove_uris = {c["uri"] for c in contacts}
                bindings[:] = [b for b in bindings
                               if b["contact"] not in remove_uris]
            else:
                for contact in contacts:
                    existing = next(
                        (b for b in bindings
                         if b["call_id"] == call_id
                         and b["contact"] == contact["uri"]),
                        None,
                    )
                    if existing is not None:
                        existing.update({
                            "q": contact["q"],
                            "cseq": cseq,
                            "expires_at": now + expires,
                            "received": received,
                            "rport": rport,
                        })
                    else:
                        bindings.append({
                            "contact": contact["uri"],
                            "q": contact["q"],
                            "call_id": call_id,
                            "cseq": cseq,
                            "expires_at": now + expires,
                            "received": received,
                            "rport": rport,
                        })

            return {
                "status": 200,
                "bindings": self._binding_list(device_id),
                "challenge": None,
                "primary_contact": self._primary(device_id),
                "replay": False,
            }

    def bindings(self, device_id):
        """Return the current binding list for a device/AOR."""
        with self._lock:
            return self._binding_list(device_id)

    def routing_target(self, device_id, call_id, contact):
        """Return the address replies must be sent to (NAT aware)."""
        with self._lock:
            for binding in self._bindings.get(device_id, []):
                if (binding["call_id"] == call_id
                        and binding["contact"] == contact):
                    if binding["received"] is not None:
                        if binding["rport"] is not None:
                            return "%s:%s" % (binding["received"],
                                              binding["rport"])
                        return binding["received"]
                    return contact
            return None

    def disconnect(self, device_id, call_id):
        """Drop all bindings for a call on transport teardown.

        Returns {"removed": int}.
        """
        with self._lock:
            bindings = self._bindings.get(device_id, [])
            kept = [b for b in bindings if b["call_id"] != call_id]
            removed = len(bindings) - len(kept)
            if kept:
                self._bindings[device_id] = kept
            else:
                self._bindings.pop(device_id, None)
            self._last_cseq.pop((device_id, call_id), None)
            return {"removed": removed}

    def expire(self, now):
        """Remove bindings past their expires time.

        Returns {"expired": int}.
        """
        with self._lock:
            expired = 0
            for device_id in list(self._bindings):
                kept = [b for b in self._bindings[device_id]
                        if b["expires_at"] > now]
                expired += len(self._bindings[device_id]) - len(kept)
                if kept:
                    self._bindings[device_id] = kept
                else:
                    del self._bindings[device_id]
            return {"expired": expired}
