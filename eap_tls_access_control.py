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
        raise NotImplementedError

    def reauth(self, mac, port, cert, now=1000):
        """Reauthenticate and revoke the old accounting session first."""
        raise NotImplementedError

    def mab_authenticate(self, mac, port, whitelist):
        """MAC Authentication Bypass with a mandatory whitelist.

        A MAC outside ``whitelist`` raises EapError("EAP_MAB_DENIED").
        """
        raise NotImplementedError

    def accounting_start(self, session_id):
        """Emit exactly one start event per session."""
        raise NotImplementedError

    def accounting_stop(self, session_id):
        """Emit exactly one stop event per session; duplicates are ignored."""
        raise NotImplementedError

    def accounting_events(self, session_id):
        """Return {"starts": int, "stops": int}."""
        raise NotImplementedError

    def session_active(self, session_id):
        """Return True while a session may still forward traffic."""
        raise NotImplementedError

    def port_authorized(self, port):
        """Return True when the port currently has an authorized session."""
        raise NotImplementedError

    def is_locked(self, mac):
        """Return True once the failure threshold was reached."""
        raise NotImplementedError

    def alarms(self):
        """Return the alarm codes raised so far."""
        raise NotImplementedError

    def eap_success(self, mac):
        """Send EAP-Success only after identity confirmation.

        Before a successful authentication raise
        EapError("EAP_PREMATURE_SUCCESS").
        """
        raise NotImplementedError

    def reassemble_fragments(self, fragments, declared_len):
        """Reassemble EAP fragments with strict length validation.

        A declared or actual length above ``max_eap_len``, or a mismatch, raises
        EapError("EAP_FRAGMENT_LENGTH_MISMATCH").
        Returns {"length": int, "data": bytes}.
        """
        raise NotImplementedError

    def restore_state(self, state):
        """Restore after a switch restart without trusting old authorizations.

        Ports must come back unauthorized (fail closed); returns
        {"restored": True, "authorized_ports": 0}.
        """
        raise NotImplementedError

    def can_access(self, mac, target_vlan):
        """Return False when a guest-VLAN MAC targets a server VLAN."""
        raise NotImplementedError
