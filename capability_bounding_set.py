"""Capability bounding-set transitions (skeleton)."""


class CapabilityError(Exception):
    """Error carrying a stable capability error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CapabilityBoundingSet:
    """Tracks effective, permitted, bounding, inheritable and ambient sets."""

    def __init__(self, kernel_version=(5, 15),
                 caps=("net_admin", "net_raw", "chown", "setuid", "kill")):
        self.kernel_version = tuple(kernel_version)
        self._effective = set(caps)
        self._permitted = set(caps)
        self._bounding = set(caps)
        self._inheritable = set()
        self._ambient = set()

    def effective_caps(self):
        """Return the effective capability set."""
        raise NotImplementedError

    def permitted_caps(self):
        """Return the permitted capability set."""
        raise NotImplementedError

    def bounding_caps(self):
        """Return the bounding capability set."""
        raise NotImplementedError

    def inheritable_caps(self):
        """Return the inheritable capability set."""
        raise NotImplementedError

    def ambient_caps(self):
        """Return the ambient capability set."""
        raise NotImplementedError

    def drop(self, cap):
        """Drop a capability everywhere.

        Unknown names raise CapabilityError("CAP_DROP_FAILED"); a dropped
        bounding capability is permanent.  Returns {"dropped": True}.
        """
        raise NotImplementedError

    def raise_cap(self, cap):
        """Raise a capability from the bounding set.

        A capability not in the bounding set raises CapabilityError("EPERM").
        Returns {"raised": True}.
        """
        raise NotImplementedError

    def transition(self, uid_changed=True, setuid=True, clear_effective=True):
        """Exec a new program, optionally after a UID switch.

        Refusing to clear effective/permitted raises
        CapabilityError("CAP_TRANSITION_DENIED").  A UID switch must clear
        effective, permitted and ambient sets.  Returns {"effective": set,
        "permitted": set, "ambient": set}.
        """
        raise NotImplementedError

    def set_ambient(self, cap):
        """Raise an ambient capability only from the permitted set.

        Otherwise raise CapabilityError("CAP_AMBIENT_DENIED").
        """
        raise NotImplementedError

    def enable_file_caps(self, caps):
        """Enable file capabilities; conflicting with no_new_privs fails.

        Raises CapabilityError("CAP_NO_NEW_PRIVS_CONFLICT").
        """
        raise NotImplementedError

    def change_uid(self, uid, allowed_caps):
        """Recompute permitted capabilities for a new uid."""
        raise NotImplementedError

    def set_inheritable(self, cap):
        """Add a capability to the inheritable set."""
        raise NotImplementedError

    def fork_child(self):
        """Fork a child and return its effective/permitted/inheritable/ambient
        sets; only inheritable and ambient may leak."""
        raise NotImplementedError

    def set_version(self, version):
        """Select the capability ABI version.

        A mismatch with the kernel version raises
        CapabilityError("CAP_VERSION_MISMATCH").
        """
        raise NotImplementedError
