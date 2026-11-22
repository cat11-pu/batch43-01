"""BGP RPKI origin validator (skeleton)."""


class BgpError(Exception):
    """Error carrying a stable RPKI validation code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class BgpOriginValidator:
    """Validates announcements against ROIs and installs only valid routes."""

    def __init__(self):
        self._generation = 0

    def load_rois(self, records):
        """Atomically replace the ROI table with ``records``.

        Each record is a dict with "prefix" (CIDR), "max_length" (int),
        "origin_as" (int) and "updated_at" (int).  A malformed batch raises
        BgpError("BGP_ROV_LOAD_FAILED") and leaves the previous table and
        generation untouched.  Returns {"loaded": int, "generation": int}.
        """
        raise NotImplementedError

    def snapshot(self):
        """Return {"generation": int, "count": int} for the current table."""
        raise NotImplementedError

    def validate(self, prefix, origin_as, path=()):
        """Return {"state": "valid"|"invalid"|"notfound", "code": str|None}.

        Invalid covers: origin AS mismatch, announced length above the ROI
        max_length, any AS repeated in ``path``, wrong address family, and a
        non-scalar origin value.  "code" is "BGP_ROV_INVALID" when invalid.
        """
        raise NotImplementedError

    def install_route(self, prefix, origin_as, path=(), local_pref=100):
        """Install a route only when validation says valid.

        Returns {"installed": bool, "state": str, "code": str|None}.  An
        invalid route must never replace an already installed valid route,
        regardless of local preference.
        """
        raise NotImplementedError

    def withdraw(self, prefix):
        """Withdraw a prefix and clear every cached validation mark.

        Returns {"withdrawn": True, "state_cleared": True}.
        """
        raise NotImplementedError

    def rov_state(self, prefix):
        """Return the current validation state: valid, invalid or unknown."""
        raise NotImplementedError

    def advertise(self):
        """Return the sorted list of prefixes safe to advertise."""
        raise NotImplementedError

    def soft_reconfigure(self, policies):
        """Rebuild the policy chain without dropping the ROV stage.

        Returns {"stages": list, "rov_enabled": True}.
        """
        raise NotImplementedError
