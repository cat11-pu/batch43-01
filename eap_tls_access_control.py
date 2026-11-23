"""EAP-TLS port access control (skeleton)."""


class EapError(Exception):
    """Error carrying a stable EAP/802.1X error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class EapTlsAccessControl:
    """Certificate validation, port authorization and accounting sessions."""

    def __init__(self, failure_threshold=5,
                 vlan_map=None, pinned_root="root-fp", guest_vlan=999,
                 server_vlans=(10, 20, 30), max_eap_len=65535):
        self.failure_threshold = failure_threshold
        self.vlan_map = dict(vlan_map or {})
        self.pinned_root = pinned_root
        self.guest_vlan = guest_vlan
        self.server_vlans = tuple(server_vlans)
        self.max_eap_len = max_eap_len
        self._sessions = {}
        self._mac_session = {}
        self._port_session = {}
        self._failures = {}
        self._locked = set()
        self._alarms = []
        self._confirmed = set()
        self._session_counter = 0

    def _alarm(self, code):
        if code not in self._alarms:
            self._alarms.append(code)

    def _record_failure(self, mac):
        count = self._failures.get(mac, 0) + 1
        self._failures[mac] = count
        if count >= self.failure_threshold and mac not in self._locked:
            self._locked.add(mac)
            self._alarm("EAP_LOCKED")

    def _validate_cert(self, cert, now):
        if not cert.get("chain_trusted", False):
            raise EapError("EAP_CERT_UNTRUSTED", "certificate chain not trusted")
        if cert.get("root_fingerprint") != self.pinned_root:
            self._alarm("EAP_ROOT_CHANGED")
            raise EapError("EAP_ROOT_CHANGED", "root fingerprint mismatch")
        if cert.get("usage") != "clientAuth":
            raise EapError("EAP_CERT_USAGE_INVALID", "missing clientAuth usage")
        not_before = cert.get("not_before", 0)
        not_after = cert.get("not_after", 0)
        if now < not_before or now > not_after:
            raise EapError("EAP_CERT_EXPIRED", "certificate outside validity window")
        identities = [cert.get("subject_cn")] + list(cert.get("san") or [])
        for identity in identities:
            if identity in self.vlan_map:
                return self.vlan_map[identity]
        raise EapError("EAP_IDENTITY_UNKNOWN", "no exact CN/SAN mapping")

    def _revoke_session(self, session_id):
        session = self._sessions.get(session_id)
        if session is None or not session["active"]:
            return
        session["active"] = False
        if session["starts"] > session["stops"]:
            session["stops"] += 1
        self._port_session.pop(session["port"], None)
        if self._mac_session.get(session["mac"]) == session_id:
            del self._mac_session[session["mac"]]

    def _authorize(self, mac, port, vlan):
        old_session_id = self._mac_session.get(mac)
        if old_session_id is not None:
            self._revoke_session(old_session_id)
        port_session_id = self._port_session.get(port)
        if port_session_id is not None:
            self._revoke_session(port_session_id)
        self._session_counter += 1
        session_id = "sess-%d" % self._session_counter
        self._sessions[session_id] = {
            "mac": mac, "port": port, "vlan": vlan,
            "active": True, "starts": 0, "stops": 0,
        }
        self._mac_session[mac] = session_id
        self._port_session[port] = session_id
        self._confirmed.add(mac)
        self._failures.pop(mac, None)
        return {
            "authorized": True,
            "vlan": vlan,
            "session_id": session_id,
            "port": port,
        }

    def authenticate(self, mac, port, cert, now=1000):
        """Authorize a port only when the whole certificate checks out.

        ``cert`` has subject_cn, san, not_before, not_after, usage,
        chain_trusted and root_fingerprint.  Checks: chain, pinned root,
        clientAuth usage, validity, exact CN/SAN mapping.  Returns
        {"authorized": True, "vlan": int, "session_id": str, "port": str}.
        A new authorization for a MAC revokes its previous port.
        Failures raise the matching code (EAP_CERT_EXPIRED,
        EAP_CERT_UNTRUSTED, EAP_CERT_USAGE_INVALID, EAP_ROOT_CHANGED,
        EAP_IDENTITY_UNKNOWN, EAP_LOCKED).
        """
        if mac in self._locked:
            raise EapError("EAP_LOCKED", "identity locked after repeated failures")
        try:
            vlan = self._validate_cert(cert, now)
        except EapError:
            self._record_failure(mac)
            raise
        return self._authorize(mac, port, vlan)

    def reauth(self, mac, port, cert, now=1000):
        """Reauthenticate and revoke the old accounting session first."""
        old_session_id = self._mac_session.get(mac)
        if old_session_id is not None:
            self._revoke_session(old_session_id)
        return self.authenticate(mac, port, cert, now=now)

    def mab_authenticate(self, mac, port, whitelist):
        """MAC Authentication Bypass with a mandatory whitelist.

        A MAC outside ``whitelist`` raises EapError("EAP_MAB_DENIED").
        """
        if mac not in whitelist:
            self._record_failure(mac)
            raise EapError("EAP_MAB_DENIED", "MAC not in whitelist")
        return self._authorize(mac, port, self.guest_vlan)

    def accounting_start(self, session_id):
        """Emit exactly one start event per session."""
        session = self._sessions.get(session_id)
        if session is None:
            raise EapError("EAP_SESSION_UNKNOWN", "unknown session")
        if session["starts"] == 0:
            session["starts"] = 1

    def accounting_stop(self, session_id):
        """Emit exactly one stop event per session; duplicates are ignored."""
        session = self._sessions.get(session_id)
        if session is None:
            raise EapError("EAP_SESSION_UNKNOWN", "unknown session")
        if session["starts"] > session["stops"]:
            session["stops"] += 1

    def accounting_events(self, session_id):
        """Return {"starts": int, "stops": int}."""
        session = self._sessions.get(session_id)
        if session is None:
            return {"starts": 0, "stops": 0}
        return {"starts": session["starts"], "stops": session["stops"]}

    def session_active(self, session_id):
        """Return True while a session may still forward traffic."""
        session = self._sessions.get(session_id)
        return bool(session and session["active"])

    def port_authorized(self, port):
        """Return True when the port currently has an authorized session."""
        session_id = self._port_session.get(port)
        return session_id is not None and self.session_active(session_id)

    def is_locked(self, mac):
        """Return True once the failure threshold was reached."""
        return mac in self._locked

    def alarms(self):
        """Return the alarm codes raised so far."""
        return list(self._alarms)

    def eap_success(self, mac):
        """Send EAP-Success only after identity confirmation.

        Before a successful authentication raise
        EapError("EAP_PREMATURE_SUCCESS").
        """
        if mac not in self._confirmed:
            raise EapError("EAP_PREMATURE_SUCCESS", "identity not confirmed")
        return {"success": True, "mac": mac}

    def reassemble_fragments(self, fragments, declared_len):
        """Reassemble EAP fragments with strict length validation.

        A declared or actual length above ``max_eap_len``, or a mismatch, raises
        EapError("EAP_FRAGMENT_LENGTH_MISMATCH").
        Returns {"length": int, "data": bytes}.
        """
        data = b"".join(fragments)
        if (declared_len > self.max_eap_len or len(data) > self.max_eap_len
                or declared_len != len(data)):
            raise EapError("EAP_FRAGMENT_LENGTH_MISMATCH",
                           "declared %d, actual %d" % (declared_len, len(data)))
        return {"length": len(data), "data": data}

    def restore_state(self, state):
        """Restore after a switch restart without trusting old authorizations.

        Ports must come back unauthorized (fail closed); returns
        {"restored": True, "authorized_ports": 0}.
        """
        for session_id in list(self._sessions):
            self._revoke_session(session_id)
        self._port_session.clear()
        self._mac_session.clear()
        return {"restored": True, "authorized_ports": 0}

    def can_access(self, mac, target_vlan):
        """Return False when a guest-VLAN MAC targets a server VLAN."""
        session_id = self._mac_session.get(mac)
        session = self._sessions.get(session_id)
        if session is None or not session["active"]:
            return False
        if session["vlan"] == self.guest_vlan and target_vlan in self.server_vlans:
            return False
        return True
