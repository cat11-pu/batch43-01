"""Capability bounding-set transitions.

The class models the five Linux capability sets (effective, permitted,
bounding, inheritable and ambient) and the state transitions that happen when
privileges are dropped, the user changes or a new program is executed.

Security invariants enforced here:

* a capability removed from the bounding set can never be raised again;
* a UID switch recomputes the permitted set and keeps narrowing the bounding
  set, so dropped capabilities cannot reappear;
* ambient capabilities never survive a set-user-ID exec;
* file capabilities are ignored while ``no_new_privs`` is set;
* a child can only receive inheritable (and ambient) capabilities, never the
  parent's effective/permitted sets;
* every state-changing operation is serialized so forking and exec'ing cannot
  observe or corrupt half-applied transitions.
"""

import threading


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
        initial = set(caps)
        self._known_caps = set(initial)
        self._effective = set(initial)
        self._permitted = set(initial)
        self._bounding = set(initial)
        self._inheritable = set()
        self._ambient = set()
        self._uid = 0
        self._no_new_privs = True
        self._file_caps = set()
        self._version = self.kernel_version
        self._lock = threading.RLock()

    def _check_known(self, cap):
        """Reject capability names the kernel was never told about."""
        if cap not in self._known_caps:
            raise CapabilityError(
                "CAP_DROP_FAILED", "unknown capability: %r" % (cap,))

    def _purge_ambient(self):
        """Ambient caps must always be a subset of permitted and inheritable."""
        self._ambient &= self._permitted
        self._ambient &= self._inheritable

    def effective_caps(self):
        """Return the effective capability set."""
        with self._lock:
            return set(self._effective)

    def permitted_caps(self):
        """Return the permitted capability set."""
        with self._lock:
            return set(self._permitted)

    def bounding_caps(self):
        """Return the bounding capability set."""
        with self._lock:
            return set(self._bounding)

    def inheritable_caps(self):
        """Return the inheritable capability set."""
        with self._lock:
            return set(self._inheritable)

    def ambient_caps(self):
        """Return the ambient capability set."""
        with self._lock:
            return set(self._ambient)

    def drop(self, cap):
        """Drop a capability everywhere.

        Unknown names raise CapabilityError("CAP_DROP_FAILED"); a dropped
        bounding capability is permanent.  Returns {"dropped": True}.
        """
        with self._lock:
            self._check_known(cap)
            # The bounding set can only shrink: removing the cap here makes
            # every later raise_cap() call fail with EPERM.
            self._bounding.discard(cap)
            self._effective.discard(cap)
            self._permitted.discard(cap)
            self._inheritable.discard(cap)
            self._ambient.discard(cap)
            self._file_caps.discard(cap)
            return {"dropped": True}

    def raise_cap(self, cap):
        """Raise a capability from the bounding set.

        A capability not in the bounding set raises CapabilityError("EPERM").
        Returns {"raised": True}.
        """
        with self._lock:
            self._check_known(cap)
            if cap not in self._bounding:
                raise CapabilityError(
                    "EPERM",
                    "capability %r is outside the bounding set" % (cap,))
            self._permitted.add(cap)
            self._effective.add(cap)
            return {"raised": True}

    def transition(self, uid_changed=True, setuid=True, clear_effective=True):
        """Exec a new program, optionally after a UID switch.

        Refusing to clear effective/permitted raises
        CapabilityError("CAP_TRANSITION_DENIED").  A UID switch must clear
        effective, permitted and ambient sets.  Returns {"effective": set,
        "permitted": set, "ambient": set}.
        """
        with self._lock:
            if uid_changed and not clear_effective:
                raise CapabilityError(
                    "CAP_TRANSITION_DENIED",
                    "effective and permitted sets must be cleared when "
                    "executing after a UID switch")

            if uid_changed:
                # A non-root process cannot restore capabilities across the
                # exec; the bounding set stays as the permanent ceiling.
                self._effective.clear()
                self._permitted.clear()

            if setuid:
                # The kernel drops ambient caps on set-user-ID execve so that
                # a privileged image cannot inherit capabilities silently.
                self._ambient.clear()

            self._purge_ambient()
            return {
                "effective": set(self._effective),
                "permitted": set(self._permitted),
                "ambient": set(self._ambient),
            }

    def set_ambient(self, cap):
        """Raise an ambient capability only from the permitted set.

        Otherwise raise CapabilityError("CAP_AMBIENT_DENIED").
        """
        with self._lock:
            self._check_known(cap)
            if cap not in self._permitted or cap not in self._bounding:
                raise CapabilityError(
                    "CAP_AMBIENT_DENIED",
                    "ambient capability %r must come from the permitted set"
                    % (cap,))
            self._inheritable.add(cap)
            self._ambient.add(cap)
            return {"ambient": set(self._ambient)}

    def enable_file_caps(self, caps):
        """Enable file capabilities; conflicting with no_new_privs fails.

        Raises CapabilityError("CAP_NO_NEW_PRIVS_CONFLICT").
        """
        with self._lock:
            if self._no_new_privs:
                raise CapabilityError(
                    "CAP_NO_NEW_PRIVS_CONFLICT",
                    "file capabilities cannot grant privileges while "
                    "no_new_privs is set")
            requested = set(caps)
            unknown = requested - self._known_caps
            if unknown:
                raise CapabilityError(
                    "CAP_DROP_FAILED",
                    "unknown file capabilities: %s" % (sorted(unknown),))
            # File caps can never exceed the bounding-set ceiling.
            granted = requested & self._bounding
            self._file_caps = set(granted)
            self._permitted |= granted
            self._effective |= granted
            return {"file_caps": set(self._file_caps)}

    def set_no_new_privs(self):
        """Set the no_new_privs bit; like the kernel bit it cannot be unset."""
        with self._lock:
            self._no_new_privs = True
            return {"no_new_privs": True}

    def change_uid(self, uid, allowed_caps):
        """Recompute permitted capabilities for a new uid."""
        with self._lock:
            requested = set(allowed_caps)
            unknown = requested - self._known_caps
            if unknown:
                raise CapabilityError(
                    "CAP_DROP_FAILED",
                    "unknown capabilities: %s" % (sorted(unknown),))

            # The keep-cap flag (PR_SET_KEEPCAPS) is what lets the surviving
            # permitted set be recomputed instead of being wiped wholesale.
            keep_caps = requested & self._bounding
            self._uid = uid
            self._permitted = set(keep_caps)
            self._effective = set(keep_caps)
            # The bounding set keeps narrowing: anything no longer permitted
            # can never be raised back after the identity change.
            self._bounding &= keep_caps
            self._inheritable &= keep_caps
            # Ambient caps only exist while the matching permitted cap does.
            self._purge_ambient()
            return {
                "uid": self._uid,
                "permitted": set(self._permitted),
            }

    def set_inheritable(self, cap):
        """Add a capability to the inheritable set."""
        with self._lock:
            self._check_known(cap)
            if cap not in self._bounding:
                raise CapabilityError(
                    "EPERM",
                    "inheritable capability %r is outside the bounding set"
                    % (cap,))
            self._inheritable.add(cap)
            return {"inheritable": set(self._inheritable)}

    def fork_child(self):
        """Fork a child and return its effective/permitted/inheritable/ambient
        sets; only inheritable and ambient may leak."""
        with self._lock:
            # The child starts with empty effective/permitted sets.  Only the
            # inheritable set (plus ambient, which is a strict subset of it)
            # is allowed to cross the fork boundary.
            child = {
                "effective": set(),
                "permitted": set(),
                "inheritable": set(self._inheritable),
                "ambient": set(self._ambient),
            }
            return child

    def set_version(self, version):
        """Select the capability ABI version.

        A mismatch with the kernel version raises
        CapabilityError("CAP_VERSION_MISMATCH").
        """
        with self._lock:
            version = tuple(version)
            # The ABI is tied to the kernel major version: a mismatch must be
            # rejected loudly instead of silently downgrading.
            if version[0] != self.kernel_version[0]:
                raise CapabilityError(
                    "CAP_VERSION_MISMATCH",
                    "capability ABI %s does not match kernel %s"
                    % (version, self.kernel_version))
            self._version = version
            return {"version": self._version}

    def version(self):
        """Return the selected capability ABI version."""
        with self._lock:
            return tuple(self._version)
