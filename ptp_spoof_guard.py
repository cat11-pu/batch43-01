"""PTP announce/Sync spoof guard."""


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
        self._announced = {}
        self._last_announce_seq = {}
        self._master = None
        self._sync_seen = set()
        self._follow_up_paired = set()
        self._follow_up_ts = {}
        self._corrections = {}
        self._ts_queue = []
        self._audit = []
        self._alarms = []

    def _record_audit(self, entry_type, source=None, **fields):
        """Append an audit entry carrying master identity and source."""
        entry = {
            "type": entry_type,
            "source": source if source is not None else self._master,
            "master_id": self._master,
        }
        entry.update(fields)
        self._audit.append(entry)
        return entry

    @staticmethod
    def _announce_rank(priority1, priority2):
        return priority1, priority2

    def _select_master(self):
        """Re-rank every accepted announce and keep a single best master."""
        if not self._announced:
            return None
        source = min(
            self._announced,
            key=lambda addr: self._announce_rank(
                self._announced[addr]["priority1"],
                self._announced[addr]["priority2"]),
        )
        if source != self._master:
            self._master = source
            info = self._announced[source]
            self._record_audit(
                "master_select",
                source=source,
                priority1=info["priority1"],
                priority2=info["priority2"],
                clock_class=info["clock_class"],
                seq=info["seq"],
            )
        return self._master

    def process_announce(self, source, priority1, priority2=128,
                         clock_class=248, seq=0):
        """Validate and rank an announce message.

        A source outside the whitelist raises
        PtpError("PTP_ANNOUNCE_REJECTED") and is written to the audit log.
        Returns {"accepted": bool, "master_id": str|None, "selected": bool};
        the best (lowest priority1, then priority2) accepted source becomes the
        single master.
        """
        if source not in self.allowed_sources:
            self._record_audit(
                "announce_reject",
                source=source,
                reason="source_not_whitelisted",
                priority1=priority1,
                priority2=priority2,
                clock_class=clock_class,
                seq=seq,
            )
            raise PtpError(
                "PTP_ANNOUNCE_REJECTED",
                "announce source %r is not in the whitelist" % source,
            )

        last_seq = self._last_announce_seq.get(source)
        if last_seq is not None and seq < last_seq:
            self._record_audit(
                "announce_reject",
                source=source,
                reason="stale_sequence",
                seq=seq,
                last_seq=last_seq,
            )
            raise PtpError(
                "PTP_ANNOUNCE_REJECTED",
                "replayed announce seq %s after seq %s" % (seq, last_seq),
            )
        self._last_announce_seq[source] = seq

        self._announced[source] = {
            "priority1": priority1,
            "priority2": priority2,
            "clock_class": clock_class,
            "seq": seq,
        }
        previous_master = self._master
        master = self._select_master()
        return {
            "accepted": True,
            "master_id": master,
            "selected": master == source and (
                previous_master != source or previous_master is None),
        }

    def best_master(self):
        """Return the currently selected master identity, or None."""
        return self._master

    def process_sync(self, source, seq):
        """Record a Sync message; one Sync per sequence number.

        A duplicate raises PtpError("PTP_SYNC_REPLAY").
        Returns {"pending": True, "seq": int}.
        """
        if seq in self._sync_seen:
            self._record_audit("sync_replay", source=source, seq=seq)
            raise PtpError(
                "PTP_SYNC_REPLAY",
                "duplicate Sync seq %s" % seq,
            )
        self._sync_seen.add(seq)
        return {"pending": True, "seq": seq}

    def process_follow_up(self, source, seq, origin_timestamp):
        """Pair a Follow_Up with the matching Sync exactly once.

        A missing Sync or a second Follow_Up for the same seq raises
        PtpError("PTP_SYNC_REPLAY").
        Returns {"paired": True, "seq": int, "origin_timestamp": int}.
        """
        if seq not in self._sync_seen or seq in self._follow_up_paired:
            self._record_audit(
                "follow_up_replay",
                source=source,
                seq=seq,
                origin_timestamp=origin_timestamp,
            )
            raise PtpError(
                "PTP_SYNC_REPLAY",
                "Follow_Up seq %s has no unmatched Sync" % seq,
            )
        self._follow_up_paired.add(seq)
        self._follow_up_ts[seq] = origin_timestamp
        return {
            "paired": True,
            "seq": seq,
            "origin_timestamp": origin_timestamp,
        }

    def apply_correction(self, seq, correction_ns):
        """Accumulate transparent-clock correction for a sequence.

        Exceeding ``correction_limit_ns`` raises
        PtpError("PTP_CORRECTION_LIMIT").  Returns {"cumulative_ns": int}.
        """
        cumulative = self._corrections.get(seq, 0) + correction_ns
        if abs(cumulative) > self.correction_limit_ns:
            self._record_audit(
                "correction_reject",
                seq=seq,
                attempted_ns=correction_ns,
                cumulative_ns=cumulative,
                limit_ns=self.correction_limit_ns,
            )
            raise PtpError(
                "PTP_CORRECTION_LIMIT",
                "cumulative correction %s ns exceeds limit %s ns"
                % (cumulative, self.correction_limit_ns),
            )
        self._corrections[seq] = cumulative
        return {"cumulative_ns": cumulative}

    def request_unicast(self, source, duration):
        """Grant a unicast session only to a whitelisted source.

        Unknown sources raise PtpError("PTP_UNICAST_SOURCE_INVALID").
        """
        if source not in self.allowed_sources:
            self._record_audit(
                "unicast_reject",
                source=source,
                duration=duration,
            )
            raise PtpError(
                "PTP_UNICAST_SOURCE_INVALID",
                "unicast request from %r is not whitelisted" % source,
            )
        return {"granted": True, "source": source, "duration": duration}

    def enqueue_timestamp(self, timestamp_ns):
        """Queue a hardware timestamp.

        A full queue raises PtpError("PTP_QUEUE_OVERFLOW") instead of silently
        dropping.  Returns {"queued": int}.
        """
        if len(self._ts_queue) >= self.queue_limit:
            self._record_audit(
                "queue_overflow",
                timestamp_ns=timestamp_ns,
                queue_size=len(self._ts_queue),
                limit=self.queue_limit,
            )
            raise PtpError(
                "PTP_QUEUE_OVERFLOW",
                "hardware timestamp queue is full (%d)" % self.queue_limit,
            )
        self._ts_queue.append(timestamp_ns)
        return {"queued": len(self._ts_queue)}

    def audit_step(self, offset_ns, threshold_ns=1000):
        """Audit a clock step and alarm above the threshold.

        Returns {"audited": True, "alarm": bool}; the audit entry carries the
        master identity and message source.
        """
        alarm = abs(offset_ns) > threshold_ns
        self._record_audit(
            "step",
            offset_ns=offset_ns,
            threshold_ns=threshold_ns,
            alarm=alarm,
        )
        if alarm:
            self._alarms.append("PTP_STEP_ALARM")
        return {"audited": True, "alarm": alarm}

    def audit_leap(self, seconds):
        """Audit a leap-second jump and alarm.

        Returns {"audited": True, "alarm": True}.
        """
        self._record_audit("leap", seconds=seconds, alarm=True)
        self._alarms.append("PTP_LEAP_ALARM")
        return {"audited": True, "alarm": True}

    def audit_log(self):
        """Return the audit entries, newest last."""
        return list(self._audit)

    def alarms(self):
        """Return the alarm codes raised so far."""
        return list(self._alarms)

    def restore_offset(self, state, now):
        """Apply a persisted offset only when it is still fresh.

        ``state`` is {"offset_ns": int, "captured_at": int}.  Stale offsets
        return {"applied": False}; fresh ones return {"applied": True}.
        """
        age = now - state["captured_at"]
        if age < 0 or age > self.max_offset_age_s:
            self._record_audit(
                "offset_restore_reject",
                offset_ns=state["offset_ns"],
                captured_at=state["captured_at"],
                age_s=age,
                max_age_s=self.max_offset_age_s,
            )
            return {"applied": False}
        return {"applied": True, "offset_ns": state["offset_ns"]}

    def verify_config(self, config_version, expected_version):
        """Reject a config template that does not match the baseline.

        Mismatch raises PtpError("PTP_CONFIG_MISMATCH").
        """
        if config_version != expected_version:
            self._record_audit(
                "config_mismatch",
                config_version=config_version,
                expected_version=expected_version,
            )
            raise PtpError(
                "PTP_CONFIG_MISMATCH",
                "config %r does not match baseline %r"
                % (config_version, expected_version),
            )
        return {"verified": True, "version": config_version}
