"""CAEP shared-signal event journal.

The journal deliberately treats an incoming event as a transaction. Validation,
replay detection, state changes, and persistence all happen while holding the
journal lock, so an invalid or replayed event cannot partially change state.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from collections.abc import Mapping


KNOWN_EVENT_TYPES = frozenset(
    {
        "session-established",
        "session-revoked",
        "credential-change",
    }
)

SUBJECT_JOURNAL_WINDOW = 600.0
STATE_VERSION = 1
_DEFAULT_WINDOW = object()


class CaepError(Exception):
    """Configuration or persistence failure raised by the journal."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class CaepEventJournal:
    """Applies CAEP events exactly once and keeps revocation tombstones."""

    def __init__(
        self,
        issuer="issuer-a",
        state_path=None,
        tombstone_ttl=3600.0,
        key_set=None,
        signature_key_set=None,
        document_ttl=300.0,
        subject_window=SUBJECT_JOURNAL_WINDOW,
        **kwargs,
    ):
        # Accept the two common names used by callers for the signing key set.
        if signature_key_set is None:
            signature_key_set = kwargs.pop("signing_key_set", None)
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            raise TypeError(f"unexpected journal argument(s): {unexpected}")

        self.issuer = issuer
        self.state_path = os.fspath(state_path) if state_path is not None else None
        self.tombstone_ttl = float(tombstone_ttl)
        self.key_set = key_set if key_set is not None else signature_key_set
        self.document_ttl = float(document_ttl)
        self.subject_window = float(subject_window)

        self._lock = threading.RLock()
        self._state = self._empty_state()
        if self.state_path is not None:
            self._state = self._read_state()

    # ------------------------------------------------------------------
    # Public event API

    def ingest(self, event, now=0.0):
        """Validate and atomically apply one event.

        Only events that pass every validation step are entered in the replay
        ledger. A rejected event therefore cannot burn an event id that a
        later, valid delivery of the same event may need.
        """

        with self._lock:
            now = self._as_time(now)
            event_id = self._event_id(event)
            if event_id is None:
                return self._rejected("SET_INVALID_EVENT", "event id is required")

            id_key = str(event_id)
            if id_key in self._state["event_ids"]:
                # Replay is an audit event, not a domain-state change.
                work = copy.deepcopy(self._state)
                work["counters"]["replays"] += 1
                self._commit(work)
                return {
                    "accepted": False,
                    "code": "SET_REPLAY",
                    "event_id": event_id,
                }

            validation_error = self._validate_event(event, now)
            if validation_error is not None:
                return validation_error

            work = copy.deepcopy(self._state)
            event_types = self._event_types(event)
            session_id = self._session_id(event)
            subject, subject_issuer = self._event_subject(event)
            stored_event = copy.deepcopy(dict(event))

            for event_type in event_types:
                apply_error = self._apply_event_type(
                    work,
                    event_type,
                    stored_event,
                    session_id,
                    subject,
                    subject_issuer,
                    now,
                )
                if apply_error is not None:
                    return apply_error

            entry = self._journal_entry(
                stored_event,
                event_types,
                session_id,
                subject,
                subject_issuer,
                now,
            )
            work["journal"].append(entry)
            work["event_ids"][id_key] = {
                "recorded_at": now,
                "event_id": event_id,
            }
            work["counters"]["accepted_events"] += 1
            self._record_subject_activity(
                work, event_types, subject, subject_issuer, event_id, now
            )

            self._commit(work)
            return {
                "accepted": True,
                "code": None,
                "event_id": event_id,
            }

    def is_active(self, session_id, now=0.0):
        """Return whether a session is active at ``now``."""

        with self._lock:
            now = self._as_time(now)
            session = self._state["sessions"].get(str(session_id))
            if not session or not session.get("active"):
                return False

            tombstone = self._state["tombstones"].get(str(session_id))
            if tombstone is not None and tombstone["expires_at"] > now:
                return False

            return True

    def journal(
        self,
        subject=None,
        now=None,
        window=_DEFAULT_WINDOW,
        issuer=None,
        subject_issuer=None,
        since=None,
    ):
        """Return accepted events, optionally for one subject and time window.

        With a subject filter the default window is ten minutes. If ``now`` is
        omitted, the newest matching journal entry is used as the reference
        time. Pass ``window=None`` to return the subject's complete history.
        """

        with self._lock:
            entries = copy.deepcopy(self._state["journal"])

        if subject is not None:
            if isinstance(subject, (tuple, list)) and len(subject) == 2:
                subject_issuer, subject = subject
            entries = [entry for entry in entries if entry.get("subject") == subject]
            if subject_issuer is None:
                subject_issuer = issuer
            if subject_issuer is not None:
                entries = [
                    entry
                    for entry in entries
                    if entry.get("subject_issuer") == subject_issuer
                ]

            if now is None and entries:
                now = max(entry["recorded_at"] for entry in entries)
            if window is _DEFAULT_WINDOW:
                window = self.subject_window
        elif issuer is not None or subject_issuer is not None:
            expected = subject_issuer if subject_issuer is not None else issuer
            entries = [
                entry
                for entry in entries
                if entry.get("subject_issuer") == expected
            ]

        if window is _DEFAULT_WINDOW:
            window = None

        if since is not None:
            cutoff = float(since)
            entries = [entry for entry in entries if entry["recorded_at"] >= cutoff]
        elif now is not None and window is not None:
            cutoff = float(now) - float(window)
            entries = [
                entry
                for entry in entries
                if cutoff <= entry["recorded_at"] <= float(now)
            ]

        return entries

    def events_for_subject(
        self,
        subject,
        now=None,
        window=_DEFAULT_WINDOW,
        issuer=None,
        subject_issuer=None,
    ):
        """Compatibility alias for :meth:`journal`."""

        return self.journal(
            subject=subject,
            now=now,
            window=window,
            issuer=issuer,
            subject_issuer=subject_issuer,
        )

    def subject_status(self, subject, issuer=None, now=None):
        """Return the merged state history for a subject, if present."""

        issuer = self.issuer if issuer is None else issuer
        with self._lock:
            value = self._state["subjects"].get(self._subject_key(issuer, subject))
            return copy.deepcopy(value) if value is not None else None

    def tombstones(self, now=None):
        """Return a copy of session tombstones, optionally pruning expired ones."""

        with self._lock:
            if now is None:
                return copy.deepcopy(self._state["tombstones"])
            now = self._as_time(now)
            return {
                session_id: copy.deepcopy(tombstone)
                for session_id, tombstone in self._state["tombstones"].items()
                if tombstone["expires_at"] > now
            }

    def counters(self):
        """Return a copy of audit counters."""

        with self._lock:
            return copy.deepcopy(self._state["counters"])

    # ------------------------------------------------------------------
    # Small document cache used by signal/configuration consumers. An expired
    # entry is refreshed through the supplied loader rather than being returned
    # as if it were still current.

    def put_document(self, key, value, now=0.0, ttl=None):
        with self._lock:
            now = self._as_time(now)
            ttl = self.document_ttl if ttl is None else float(ttl)
            work = copy.deepcopy(self._state)
            work["documents"][str(key)] = {
                "value": copy.deepcopy(value),
                "stored_at": now,
                "expires_at": now + ttl,
            }
            self._commit(work)
            return copy.deepcopy(value)

    def get_document(self, key, now=0.0, loader=None, ttl=None):
        """Return a cached document, refreshing it when its TTL has elapsed."""

        with self._lock:
            now = self._as_time(now)
            key = str(key)
            document = self._state["documents"].get(key)
            if document is not None and document["expires_at"] > now:
                return copy.deepcopy(document["value"])

            if loader is None:
                return None

            value = loader()
            ttl = self.document_ttl if ttl is None else float(ttl)
            work = copy.deepcopy(self._state)
            work["documents"][key] = {
                "value": copy.deepcopy(value),
                "stored_at": now,
                "expires_at": now + ttl,
            }
            self._commit(work)
            return copy.deepcopy(value)

    def refresh_document(self, key, loader, now=0.0, ttl=None):
        return self.get_document(key, now=now, loader=loader, ttl=ttl)

    def documents(self):
        with self._lock:
            return copy.deepcopy(self._state["documents"])

    cache_document = put_document
    set_document = put_document
    load_document = get_document

    # ------------------------------------------------------------------
    # Validation

    def _validate_event(self, event, now):
        if not isinstance(event, Mapping):
            return self._rejected("SET_INVALID_EVENT", "event must be a mapping")

        if not event.get("signature_valid", True):
            return self._rejected(
                "SET_SIGNATURE_INVALID", "event signature is invalid"
            )

        event_key_set = self._event_key_set(event)
        if (
            self.key_set is not None
            and event_key_set != self.key_set
        ):
            return self._rejected(
                "SET_SIGNATURE_INVALID", "event was signed with the wrong key set"
            )

        event_issuer = event.get("issuer", event.get("iss"))
        if event_issuer != self.issuer:
            return self._rejected(
                "SET_ISSUER_MISMATCH", "event issuer is not the configured issuer"
            )

        subject, subject_issuer = self._event_subject(event)
        if (
            subject is not None
            and subject_issuer is not None
            and subject_issuer != self.issuer
        ):
            return self._rejected(
                "SET_SUBJECT_MISMATCH",
                "event subject issuer is not the configured issuer",
            )

        event_types = self._event_types(event)
        if not event_types:
            return self._rejected(
                "SET_INVALID_EVENT", "event must contain at least one type"
            )
        if any(
            not isinstance(event_type, str)
            or event_type not in KNOWN_EVENT_TYPES
            for event_type in event_types
        ):
            return self._rejected(
                "SET_UNKNOWN_TYPE", "event contains an unknown type"
            )

        session_id = self._session_id(event)
        requires_session = any(
            event_type in {"session-established", "session-revoked"}
            for event_type in event_types
        )
        if requires_session and session_id is None:
            return self._rejected(
                "SET_INVALID_EVENT", "session event requires a session id"
            )

        expires_at = self._as_optional_time(event.get("exp"))
        if expires_at is not None and now >= expires_at:
            return self._rejected("SET_EXPIRED", "event has expired")
        return None

    # ------------------------------------------------------------------
    # Event application. These methods only mutate the transaction copy.

    def _apply_event_type(
        self,
        work,
        event_type,
        event,
        session_id,
        subject,
        subject_issuer,
        now,
    ):
        if event_type == "session-established":
            return self._apply_established(
                work, event, session_id, subject, subject_issuer, now
            )
        if event_type == "session-revoked":
            return self._apply_revoked(
                work, event, session_id, subject, subject_issuer, now
            )
        if event_type == "credential-change":
            return self._apply_credential_change(
                work, session_id, subject, subject_issuer, now
            )
        return self._rejected("SET_UNKNOWN_TYPE", "unknown event type")

    def _apply_established(
        self, work, event, session_id, subject, subject_issuer, now
    ):
        session_key = str(session_id)
        tombstone = work["tombstones"].get(session_key)
        if tombstone is not None:
            if tombstone["expires_at"] > now:
                return self._rejected(
                    "SET_TOMBSTONED",
                    "session id is inside an active revocation window",
                )
            del work["tombstones"][session_key]

        session = work["sessions"].get(session_key)
        mismatch = self._subject_mismatch(session, subject, subject_issuer)
        if mismatch is not None:
            return mismatch

        if session is None:
            session = {
                "session_id": session_id,
                "subject": None,
                "subject_issuer": None,
            }
            work["sessions"][session_key] = session

        if subject is not None:
            session["subject"] = subject
        if subject_issuer is not None:
            session["subject_issuer"] = subject_issuer
        session["active"] = True
        session["established_at"] = now
        session["established_iat"] = event.get("iat")
        session["event_exp"] = self._as_optional_time(event.get("exp"))
        session["revoked_at"] = None
        session["last_event_id"] = event.get("id")
        session["last_event_at"] = now
        return None

    def _apply_revoked(
        self, work, event, session_id, subject, subject_issuer, now
    ):
        session_key = str(session_id)
        session = work["sessions"].get(session_key)
        mismatch = self._subject_mismatch(session, subject, subject_issuer)
        if mismatch is not None:
            return mismatch

        if session is None:
            session = {
                "session_id": session_id,
                "subject": subject,
                "subject_issuer": subject_issuer,
                "established_at": None,
                "established_iat": None,
            }
            work["sessions"][session_key] = session

        if subject is not None and session.get("subject") is None:
            session["subject"] = subject
            session["subject_issuer"] = subject_issuer

        session["active"] = False
        session["revoked_at"] = now
        session["last_event_id"] = event.get("id")
        session["last_event_at"] = now

        expires_at = now + max(0.0, self.tombstone_ttl)
        previous = work["tombstones"].get(session_key)
        if previous is None:
            work["tombstones"][session_key] = {
                "session_id": session_id,
                "subject": session.get("subject"),
                "subject_issuer": session.get("subject_issuer"),
                "revoked_at": now,
                "expires_at": expires_at,
                "event_id": event.get("id"),
            }
        else:
            # Revocation is monotonic: a stale/out-of-order event must never
            # shorten an existing tombstone or replace its subject.
            previous["revoked_at"] = min(previous["revoked_at"], now)
            previous["expires_at"] = max(previous["expires_at"], expires_at)
            previous["event_id"] = event.get("id")

        work["counters"]["revocations"] += 1
        return None

    def _apply_credential_change(
        self, work, session_id, subject, subject_issuer, now
    ):
        if session_id is not None:
            session = work["sessions"].get(str(session_id))
            mismatch = self._subject_mismatch(session, subject, subject_issuer)
            if mismatch is not None:
                return mismatch

        work["counters"]["credential_changes"] += 1
        return None

    def _subject_mismatch(self, session, subject, subject_issuer):
        if session is None:
            return None

        session_subject = session.get("subject")
        session_issuer = session.get("subject_issuer")
        if (
            subject is not None
            and session_subject is not None
            and subject != session_subject
        ):
            return self._rejected(
                "SET_SUBJECT_MISMATCH",
                "event subject does not match the session subject",
            )
        if (
            subject_issuer is not None
            and session_issuer is not None
            and subject_issuer != session_issuer
        ):
            return self._rejected(
                "SET_SUBJECT_MISMATCH",
                "event subject issuer does not match the session subject issuer",
            )
        return None

    # ------------------------------------------------------------------
    # Journal and subject state

    def _journal_entry(
        self,
        event,
        event_types,
        session_id,
        subject,
        subject_issuer,
        now,
    ):
        entry = copy.deepcopy(event)
        entry.update(
            {
                "id": event.get("id"),
                "types": list(event_types),
                "session_id": session_id,
                "issuer": self.issuer,
                "subject": subject,
                "subject_issuer": subject_issuer,
                "recorded_at": now,
                "received_at": now,
                "event": copy.deepcopy(event),
            }
        )
        return entry

    def _record_subject_activity(
        self, work, event_types, subject, subject_issuer, event_id, now
    ):
        if subject is None:
            return
        issuer = subject_issuer or self.issuer
        key = self._subject_key(issuer, subject)
        state = work["subjects"].setdefault(
            key,
            {
                "issuer": issuer,
                "subject": subject,
                "first_event_at": now,
                "last_event_at": now,
                "last_established_at": None,
                "last_revoked_at": None,
                "last_credential_change_at": None,
                "last_event_id": None,
                "event_count": 0,
                "revoked": False,
            },
        )
        state["first_event_at"] = min(state["first_event_at"], now)
        state["last_event_at"] = max(state["last_event_at"], now)
        state["last_event_id"] = event_id
        state["event_count"] += 1
        if "session-established" in event_types:
            state["last_established_at"] = self._latest_time(
                state["last_established_at"], now
            )
        if "session-revoked" in event_types:
            state["last_revoked_at"] = self._latest_time(
                state["last_revoked_at"], now
            )
            state["revoked"] = True
        if "credential-change" in event_types:
            state["last_credential_change_at"] = self._latest_time(
                state["last_credential_change_at"], now
            )

    @staticmethod
    def _subject_key(issuer, subject):
        return json.dumps([issuer, subject], separators=(",", ":"), default=str)

    # ------------------------------------------------------------------
    # Persistence

    def _empty_state(self):
        return {
            "version": STATE_VERSION,
            "event_ids": {},
            "journal": [],
            "sessions": {},
            "tombstones": {},
            "subjects": {},
            "documents": {},
            "counters": {
                "accepted_events": 0,
                "replays": 0,
                "revocations": 0,
                "credential_changes": 0,
            },
        }

    def _read_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as stream:
                state = json.load(stream)
        except FileNotFoundError:
            return self._empty_state()
        except (OSError, ValueError) as exc:
            raise CaepError(
                "SET_STATE_INVALID", f"cannot read journal state: {exc}"
            ) from exc

        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise CaepError(
                "SET_STATE_INVALID", "unsupported journal state version"
            )

        empty = self._empty_state()
        for key, default in empty.items():
            if key == "version":
                continue
            if key not in state:
                state[key] = copy.deepcopy(default)
            elif isinstance(default, dict) and not isinstance(state[key], dict):
                raise CaepError(
                    "SET_STATE_INVALID", f"invalid journal state field: {key}"
                )
            elif isinstance(default, list) and not isinstance(state[key], list):
                raise CaepError(
                    "SET_STATE_INVALID", f"invalid journal state field: {key}"
                )

        for key, default in empty["counters"].items():
            state["counters"].setdefault(key, default)
        return state

    def _commit(self, work):
        if self.state_path is not None:
            self._write_state(work)
        self._state = work

    def _write_state(self, state):
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=directory,
                prefix=".caep-journal-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.state_path)
            temporary = None
        except (OSError, TypeError, ValueError) as exc:
            raise CaepError(
                "SET_STATE_WRITE_FAILED", f"cannot persist journal state: {exc}"
            ) from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Input normalisation

    @staticmethod
    def _event_id(event):
        if not isinstance(event, Mapping):
            return None
        event_id = event.get("id")
        if event_id is None or isinstance(event_id, bool):
            return None
        if isinstance(event_id, str):
            return event_id or None
        if isinstance(event_id, (int, float)):
            return event_id
        return None

    @staticmethod
    def _event_types(event):
        if not isinstance(event, Mapping):
            return []
        types = event.get("types")
        if types is None and "event_type" in event:
            types = event.get("event_type")
        if isinstance(types, str):
            return [types]
        if isinstance(types, (list, tuple)):
            return list(types)
        return []

    @staticmethod
    def _session_id(event):
        session_id = event.get("session_id", event.get("session"))
        if session_id is None:
            return None
        if isinstance(session_id, str):
            return session_id or None
        if isinstance(session_id, (int, float)):
            return session_id
        return None

    @staticmethod
    def _event_subject(event):
        subject = event.get("subject")
        if isinstance(subject, Mapping):
            subject_issuer = subject.get("issuer", subject.get("iss"))
            subject = subject.get("user", subject.get("id", subject.get("sub")))
        else:
            subject_issuer = event.get("subject_issuer", event.get("subject_iss"))
        if subject_issuer is None:
            subject_issuer = event.get("issuer", event.get("iss"))
        return subject, subject_issuer

    @staticmethod
    def _event_key_set(event):
        for key in (
            "signature_key_set",
            "key_set",
            "key_set_id",
            "signing_key_set",
            "signature_key_id",
            "kid",
        ):
            if key in event:
                return event[key]
        return None

    @staticmethod
    def _as_time(value):
        if isinstance(value, bool):
            raise TypeError("time must be numeric")
        return float(value)

    @classmethod
    def _as_optional_time(cls, value):
        if value is None:
            return None
        return cls._as_time(value)

    @staticmethod
    def _latest_time(current, candidate):
        if current is None:
            return candidate
        return max(current, candidate)

    @staticmethod
    def _rejected(code, message):
        return {
            "accepted": False,
            "code": code,
            "message": message,
        }
