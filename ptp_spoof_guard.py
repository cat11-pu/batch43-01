"""PTP announce/Sync spoof guard (skeleton)."""


class PtpError(Exception):
    """Error carrying a stable PTP guard code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class PtpSpoofGuard:
    """Validates PTP announce sources and binds Sync/Follow_Up state."""

    def __init__(self, allowed_sources=("10.0.0.1",),
                 correction_limit_ns=1000000, queue_limit=64,
                 max_offset_age_s=300):
        self.allowed_sources = tuple(allowed_sources)
        self.correction_limit_ns = correction_limit_ns
        self.queue_limit = queue_limit
        self.max_offset_age_s = max_offset_age_s

    def process_announce(self, source, priority1, priority2=128,
                         clock_class=248, seq=0):
        """Validate and rank an announce message.

        A source outside the whitelist raises
        PtpError("PTP_ANNOUNCE_REJECTED") and is written to the audit log.
        Returns {"accepted": bool, "master_id": str|None, "selected": bool};
        the best (lowest priority1, then priority2) accepted source becomes the
        single master.
        """
        raise NotImplementedError

    def best_master(self):
        """Return the currently selected master identity, or None."""
        raise NotImplementedError

    def process_sync(self, source, seq):
        """Record a Sync message; one Sync per sequence number.

        A duplicate raises PtpError("PTP_SYNC_REPLAY").
        Returns {"pending": True, "seq": int}.
        """
        raise NotImplementedError

    def process_follow_up(self, source, seq, origin_timestamp):
        """Pair a Follow_Up with the matching Sync exactly once.

        A missing Sync or a second Follow_Up for the same seq raises
        PtpError("PTP_SYNC_REPLAY").
        Returns {"paired": True, "seq": int, "origin_timestamp": int}.
        """
        raise NotImplementedError

    def apply_correction(self, seq, correction_ns):
        """Accumulate transparent-clock correction for a sequence.

        Exceeding ``correction_limit_ns`` raises
        PtpError("PTP_CORRECTION_LIMIT").  Returns {"cumulative_ns": int}.
        """
        raise NotImplementedError

    def request_unicast(self, source, duration):
        """Grant a unicast session only to a whitelisted source.

        Unknown sources raise PtpError("PTP_UNICAST_SOURCE_INVALID").
        """
        raise NotImplementedError

    def enqueue_timestamp(self, timestamp_ns):
        """Queue a hardware timestamp.

        A full queue raises PtpError("PTP_QUEUE_OVERFLOW") instead of silently
        dropping.  Returns {"queued": int}.
        """
        raise NotImplementedError

    def audit_step(self, offset_ns, threshold_ns=1000):
        """Audit a clock step and alarm above the threshold.

        Returns {"audited": True, "alarm": bool}; the audit entry carries the
        master identity and message source.
        """
        raise NotImplementedError

    def audit_leap(self, seconds):
        """Audit a leap-second jump and alarm.

        Returns {"audited": True, "alarm": True}.
        """
        raise NotImplementedError

    def audit_log(self):
        """Return the audit entries, newest last."""
        raise NotImplementedError

    def alarms(self):
        """Return the alarm codes raised so far."""
        raise NotImplementedError

    def restore_offset(self, state, now):
        """Apply a persisted offset only when it is still fresh.

        ``state`` is {"offset_ns": int, "captured_at": int}.  Stale offsets
        return {"applied": False}; fresh ones return {"applied": True}.
        """
        raise NotImplementedError

    def verify_config(self, config_version, expected_version):
        """Reject a config template that does not match the baseline.

        Mismatch raises PtpError("PTP_CONFIG_MISMATCH").
        """
        raise NotImplementedError
