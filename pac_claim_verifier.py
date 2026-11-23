"""PAC claim and signature verification.

The verifier sits on every domain logon path and must fail closed: any
unverified claim is dropped before a token is built. Decision codes are
stable strings so domain controllers and the audit pipeline can correlate
rejections across both forests without a service window.
"""

import copy
import threading
from collections import deque

PAC_SIGNATURE_INVALID = "PAC_SIGNATURE_INVALID"
PAC_SID_FILTERED = "PAC_SID_FILTERED"
PAC_BUFFER_OVERLAP = "PAC_BUFFER_OVERLAP"
PAC_BUFFER_OVERFLOW = "PAC_BUFFER_OVERFLOW"
PAC_DUPLICATE_BUFFER = "PAC_DUPLICATE_BUFFER"
PAC_EMPTY = "PAC_EMPTY"
PAC_ISSUER_UNTRUSTED = "PAC_ISSUER_UNTRUSTED"
PAC_REVALIDATE_REQUIRED = "PAC_REVALIDATE_REQUIRED"
PAC_KEY_VERSION = "PAC_KEY_VERSION"
PAC_TIME_INVALID = "PAC_TIME_INVALID"

# Well-known privileged RIDs. SID history carrying one of these from an
# untrusted forest is an escalation attempt, not a filterable claim.
PRIVILEGED_RIDS = frozenset(
    {
        512,  # Domain Admins
        518,  # Schema Admins
        519,  # Enterprise Admins
        544,  # Administrators
    }
)


class PacError(Exception):
    """Configuration failure raised by the verifier."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class PacClaimVerifier:
    """Validates PAC signatures, buffers and SID history before building tokens."""

    def __init__(
        self,
        trusted_forests=("corp.example",),
        max_age=3600.0,
        accepted_key_versions=(1, 2),
        require_service_sig=True,
        audit_log_size=4096,
    ):
        if not trusted_forests:
            raise PacError("PAC_CONFIG", "at least one trusted forest is required")
        if not isinstance(max_age, (int, float)) or max_age <= 0:
            raise PacError("PAC_CONFIG", "max_age must be a positive number")
        if not accepted_key_versions:
            raise PacError("PAC_CONFIG", "at least one accepted key version is required")

        self.trusted_forests = tuple(trusted_forests)
        self.max_age = float(max_age)
        self.accepted_key_versions = tuple(accepted_key_versions)
        # The service (server) signature is mandatory and cannot be disabled:
        # the parameter is kept for API compatibility with the old forest.
        self.require_service_sig = True

        self._lock = threading.RLock()
        self._audit_log = deque(maxlen=audit_log_size)

    def verify(self, pac, now=0.0):
        # Snapshot the caller's object first. The parse thread and the cache
        # thread may share the same PAC dict; every check below reads the
        # snapshot, so a half-swapped object can never reach token building.
        snapshot = self._snapshot(pac)
        if snapshot is None:
            decision = self._reject(
                PAC_EMPTY, "PAC object changed while it was being captured"
            )
            self._record_audit({}, decision)
            return decision
        decision = self._verify_snapshot(snapshot, float(now))
        self._record_audit(snapshot, decision)
        return decision

    @staticmethod
    def _snapshot(pac):
        if not isinstance(pac, dict):
            return {}
        for _ in range(5):
            try:
                return copy.deepcopy(pac)
            except RuntimeError:
                # Another thread mutated the PAC graph while deepcopy was
                # walking it. Retry briefly; if the object keeps changing we
                # fail closed instead of reading a half-swapped certificate.
                continue
        return None

    def audit_trail(self):
        """Return captured audit entries, oldest first, as a stable tuple."""
        with self._lock:
            return tuple(copy.deepcopy(entry) for entry in self._audit_log)

    def _verify_snapshot(self, pac, now):
        if not pac or pac.get("empty") is True:
            return self._reject(PAC_EMPTY, "empty PAC carries no verifiable claims")

        if pac.get("issuer") not in self.trusted_forests:
            return self._reject(
                PAC_ISSUER_UNTRUSTED, "PAC issuer is not a trusted forest"
            )

        if pac.get("key_version") not in self.accepted_key_versions:
            return self._reject(
                PAC_KEY_VERSION, "KDC signature uses an unaccepted key version"
            )

        # Both signatures must verify. There is no fallback to unverified
        # group information: failure here means an empty token, never the
        # groups embedded in the PAC.
        if pac.get("kdc_signature_valid") is not True:
            return self._reject(PAC_SIGNATURE_INVALID, "KDC signature failed")
        if pac.get("service_signature_valid") is not True:
            return self._reject(PAC_SIGNATURE_INVALID, "service signature failed")

        buffer_error = self._validate_buffers(pac.get("buffers"))
        if buffer_error is not None:
            reason = {
                PAC_BUFFER_OVERFLOW: "buffer offset/length exceeds its data",
                PAC_DUPLICATE_BUFFER: "duplicate buffer type present",
                PAC_BUFFER_OVERLAP: "buffer offsets overlap",
            }[buffer_error]
            return self._reject(buffer_error, reason)

        logon_time = pac.get("logon_time")
        if not isinstance(logon_time, (int, float)) or logon_time > now:
            return self._reject(PAC_TIME_INVALID, "logon timestamp is in the future")

        issued_at = pac.get("issued_at")
        if not isinstance(issued_at, (int, float)) or now - issued_at > self.max_age:
            # Stale PAC (for example a renewed ticket after the user changed
            # groups) must be re-validated against the KDC, never reused.
            return self._reject(
                PAC_REVALIDATE_REQUIRED, "PAC age exceeds policy maximum"
            )

        kept_sids, filtered_sids, privileged_leak = self._filter_sid_history(
            pac.get("sid_history")
        )
        if privileged_leak:
            return self._reject(
                PAC_SID_FILTERED,
                "SID history from an untrusted forest contains a privileged SID",
                filtered_sids=filtered_sids,
            )

        groups = list(pac.get("groups") or ())
        groups.extend(kept_sids)
        return {
            "authenticated": True,
            "code": None,
            "groups": groups,
            "user": pac.get("user_name"),
            "issuer": pac.get("issuer"),
            "filtered_sids": filtered_sids,
            "reason": None,
        }

    def _validate_buffers(self, buffers):
        if not isinstance(buffers, list):
            return PAC_BUFFER_OVERFLOW

        seen_types = set()
        spans = []
        for buffer in buffers:
            if not isinstance(buffer, dict):
                return PAC_BUFFER_OVERFLOW
            offset = buffer.get("offset")
            length = buffer.get("length")
            data = buffer.get("data", b"")
            if (
                not isinstance(offset, int)
                or not isinstance(length, int)
                or not isinstance(data, (bytes, bytearray))
                or offset < 0
                or length < 0
                or offset + length > len(data)
            ):
                return PAC_BUFFER_OVERFLOW

            buffer_type = buffer.get("type")
            if buffer_type in seen_types:
                return PAC_DUPLICATE_BUFFER
            seen_types.add(buffer_type)

            if length > 0:
                spans.append((offset, offset + length))

        # Gaps between spans are alignment padding and are ignored; only
        # actual overlap (a byte claimed by two buffers) is rejected.
        spans.sort()
        for (_, previous_end), (start, _) in zip(spans, spans[1:]):
            if start < previous_end:
                return PAC_BUFFER_OVERLAP
        return None

    def _filter_sid_history(self, sid_history):
        kept_sids = []
        filtered_sids = []
        privileged_leak = False

        for entry in sid_history or []:
            if not isinstance(entry, dict):
                filtered_sids.append(None)
                continue
            sid = entry.get("sid")
            forest = entry.get("forest")
            if forest in self.trusted_forests:
                kept_sids.append(sid)
                continue
            filtered_sids.append(sid)
            if self._rid_of(sid) in PRIVILEGED_RIDS:
                privileged_leak = True

        return kept_sids, filtered_sids, privileged_leak

    @staticmethod
    def _rid_of(sid):
        try:
            return int(str(sid).rsplit("-", 1)[1])
        except (ValueError, IndexError, AttributeError):
            return None

    def _reject(self, code, reason, filtered_sids=None):
        return {
            "authenticated": False,
            "code": code,
            "groups": [],
            "user": None,
            "issuer": None,
            "filtered_sids": list(filtered_sids or []),
            "reason": reason,
        }

    def _record_audit(self, snapshot, decision):
        entry = {
            "at": snapshot.get("logon_time"),
            "issuer": snapshot.get("issuer"),
            "user": snapshot.get("user_name"),
            "authenticated": decision["authenticated"],
            "code": decision["code"],
            "groups": list(decision.get("groups") or []),
            "presented_groups": list(snapshot.get("groups") or []),
            "filtered_sids": list(decision.get("filtered_sids") or []),
        }
        with self._lock:
            self._audit_log.append(entry)
