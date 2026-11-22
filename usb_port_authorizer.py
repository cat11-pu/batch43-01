"""USB port authorization (skeleton)."""


class UsbError(Exception):
    """Error carrying a stable USB policy error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class UsbPortAuthorizer:
    """Authorizes USB devices per interface before any driver binds."""

    def __init__(self):
        self._generation = 0

    def load_rules(self, rules):
        """Atomically replace the rule set.

        Rules need id, vid and pid; a serial-only rule raises
        UsbError("USB_RULE_INVALID") and keeps the previous policy.  Returns
        {"loaded": int, "generation": int}.
        """
        raise NotImplementedError

    def authorize(self, port_path, device):
        """Decide a device before driver binding.

        ``device`` is None until descriptors are read and raises
        UsbError("USB_DESCRIPTOR_REQUIRED"); a released port raises
        UsbError("USB_PORT_RELEASED").  Composite devices need every interface
        allowed; one denied interface denies the whole device.  Returns
        {"authorized": bool, "driver_bound": bool, "rule_id": str|None}.
        """
        raise NotImplementedError

    def release_port(self, port_path):
        """Mark a port object as released; returns {"released": True}."""
        raise NotImplementedError

    def device_present(self, port_path):
        """Return whether a device is still attached to the port."""
        raise NotImplementedError

    def is_authorized(self, port_path):
        """Return whether the port currently holds an authorized device."""
        raise NotImplementedError

    def suspend(self):
        """Suspend authorization state for sleep."""
        raise NotImplementedError

    def resume(self):
        """Resume and require reauthorization of every attached device.

        Returns {"reauthorize_ports": list, "cleared": int}.
        """
        raise NotImplementedError

    def audit_log(self):
        """Return authorization records including the port path."""
        raise NotImplementedError
