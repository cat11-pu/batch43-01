"""Kernel audit rule dispatcher."""

import copy
import os
import threading


class AuditError(Exception):
    """Error carrying a stable audit error code."""

    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuditRuleDispatcher:
    """Queues audit events and evaluates ordered rules."""

    def __init__(self, queue_size=100, exclusions=()):
        self.queue_size = queue_size
        self._exclusions = []
        if isinstance(exclusions, (str, bytes, os.PathLike)):
            exclusions = (exclusions,)
        for path in exclusions:
            self._add_exclusion_unlocked(path)
        self._rules = ()
        self._generation = 0
        self._disk_full = False
        self._running = True
        self._journal = []
        self._overflow_records = []
        self._pending_events = 0
        self._unflushed = 0
        self._multipart = {}
        self._watches = {}
        self._lock = threading.RLock()

    def enqueue(self, event):
        """Queue an audit event.

        A full queue fails closed for security events
        (AuditError("AUDIT_BACKLOG_OVERFLOW")) and records a drop code for
        others.  Disk-full raises AuditError("AUDIT_DISK_FULL") while the
        daemon stays running.  Returns {"queued": int, "code": str|None,
        "dropped": bool}.
        """
        with self._lock:
            if self._disk_full:
                raise AuditError("AUDIT_DISK_FULL")

            event = self.normalize_event(event)
            path = event.get("path") if isinstance(event, dict) else None
            if path is not None and self.is_excluded(path):
                return {"queued": 0, "code": None, "dropped": False}

            security = self._is_security_event(event)
            if self._queue_full():
                marker = {
                    "type": "AUDIT_BACKLOG_OVERFLOW",
                    "code": "AUDIT_BACKLOG_OVERFLOW",
                    "message": "AUDIT_BACKLOG_OVERFLOW",
                    "dropped": True,
                    "security": security,
                }
                self._overflow_records.append(marker)
                if security:
                    raise AuditError("AUDIT_BACKLOG_OVERFLOW")
                return {
                    "queued": 0,
                    "code": "AUDIT_BACKLOG_OVERFLOW",
                    "dropped": True,
                }

            self._append_journal(event, counts_as_event=True)
            return {"queued": 1, "code": None, "dropped": False}

    def log_entries(self):
        """Return the queued events in order."""
        with self._lock:
            return copy.deepcopy(self._journal)

    def add_rule(self, rule):
        """Add a rule; re-adding the same id is idempotent."""
        rule = copy.deepcopy(rule)
        rule_id = rule.get("id") if isinstance(rule, dict) else None
        with self._lock:
            if rule_id is not None:
                for existing in self._rules:
                    if existing.get("id") == rule_id:
                        return {"rules": len(self._rules)}
            self._rules = self._rules + (rule,)
            self._generation += 1
            return {"rules": len(self._rules)}

    def reload(self, rules):
        """Atomically replace all rules.

        Returns {"rules": int, "generation": int}; the old set must not linger.
        """
        replacement = self._deduplicate_rules(rules)
        with self._lock:
            self._rules = replacement
            self._generation += 1
            return {
                "rules": len(self._rules),
                "generation": self._generation,
            }

    def dispatch(self, event):
        """Evaluate every rule without short-circuiting.

        A never-match rule must not hide a later always-record rule.  Returns
        {"matched": list[str], "recorded": bool}.
        """
        event = self.normalize_event(event)
        with self._lock:
            rules = self._rules

        matched = []
        recorded = False
        for rule in rules:
            if not self._rule_matches(rule, event):
                continue
            rule_id = rule.get("id") if isinstance(rule, dict) else None
            if rule_id is not None:
                matched.append(rule_id)
            if self._rule_records(rule):
                recorded = True

        if recorded:
            self.enqueue(event)
        return {"matched": matched, "recorded": recorded}

    def rename_watch(self, old_path, new_path):
        """Move a watched directory inode to its new path."""
        old_path = self._normalize_path(old_path)
        new_path = self._normalize_path(new_path)
        if not old_path or not new_path:
            return

        with self._lock:
            updated = {}
            moved = False
            for watched in self._watches:
                if self._is_path_within(watched, old_path):
                    moved = True
                    relative = os.path.relpath(watched, old_path)
                    relocated = (
                        new_path
                        if relative == os.curdir
                        else os.path.join(new_path, relative)
                    )
                    updated[relocated] = self._watches[watched]
                else:
                    updated[watched] = self._watches[watched]
            if not moved:
                updated[new_path] = None
            self._watches = updated

    def watch_paths(self):
        """Return the currently watched paths."""
        with self._lock:
            return list(self._watches)

    def add_exclusion(self, path):
        """Add an exclusion path."""
        with self._lock:
            self._add_exclusion_unlocked(path)

    def is_excluded(self, path):
        """Exclude a path and its children, but not a sibling prefix."""
        path = self._normalize_path(path)
        if not path:
            return False
        with self._lock:
            for excluded in self._exclusions:
                if self._is_path_within(path, excluded):
                    return True
        return False

    def record_multipart(self, event_id, part, total):
        """Assemble multi-part audit records.

        Returns {"complete": bool, "parts": int}; the assembled record is
        written once when the last part arrives.
        """
        try:
            total = int(total)
        except (TypeError, ValueError):
            raise ValueError("total must be an integer")
        total = max(total, 1)

        with self._lock:
            state = self._multipart.get(event_id)
            if state is None:
                state = {
                    "total": total,
                    "parts": [],
                    "complete": False,
                    "recorded": False,
                }
                self._multipart[event_id] = state
            elif state["complete"]:
                return {"complete": True, "parts": len(state["parts"])}
            else:
                state["total"] = max(state["total"], total)

            state["parts"].append(copy.deepcopy(part))
            state["complete"] = len(state["parts"]) >= state["total"]

            if state["complete"] and not state["recorded"]:
                record = {
                    "type": "multipart",
                    "id": event_id,
                    "event_id": event_id,
                    "parts": copy.deepcopy(state["parts"]),
                }
                self._append_journal(record, counts_as_event=True)
                state["recorded"] = True

            return {
                "complete": state["complete"],
                "parts": len(state["parts"]),
            }

    def multipart_incomplete(self):
        """Return ids of multi-part events still missing parts."""
        with self._lock:
            return [
                event_id
                for event_id, state in self._multipart.items()
                if not state["complete"]
            ]

    def normalize_event(self, event):
        """Normalize an event, recording auid="unset" for cron jobs."""
        normalized = copy.deepcopy(event)
        if not isinstance(normalized, dict):
            return normalized

        event_type = str(normalized.get("type", "")).lower()
        is_cron = bool(normalized.get("cron")) or "cron" in event_type
        if is_cron and normalized.get("auid") in (
            None,
            "",
            -1,
            "-1",
            "4294967295",
        ):
            normalized["auid"] = "unset"
        return normalized

    def classify_arch(self, arch):
        """Classify syscall architecture as b32 or b64, never drop b32."""
        if isinstance(arch, int):
            if arch & 0x80000000:
                return {"arch": "b64"}
            if arch:
                return {"arch": "b32"}

        value = str(arch).strip().lower()
        if value.startswith("0x"):
            try:
                return self.classify_arch(int(value, 16))
            except ValueError:
                pass

        architectures_32 = {
            "i386",
            "i486",
            "i586",
            "i686",
            "x86",
            "arm",
            "armhf",
            "armv7l",
            "ppc",
            "s390",
            "riscv32",
            "mips",
            "mipsel",
            "sparc",
            "compat",
            "b32",
            "32",
        }
        architectures_64 = {
            "x86_64",
            "amd64",
            "aarch64",
            "arm64",
            "ppc64",
            "ppc64le",
            "s390x",
            "riscv64",
            "mips64",
            "mips64el",
            "sparc64",
            "b64",
            "64",
        }
        if value in architectures_32:
            return {"arch": "b32"}
        if value in architectures_64:
            return {"arch": "b64"}
        if value.endswith("64") or "x86_64" in value:
            return {"arch": "b64"}
        if value.endswith("32"):
            return {"arch": "b32"}
        return {"arch": "unknown"}

    def mark_disk_full(self):
        """Mark the audit disk as full."""
        with self._lock:
            self._disk_full = True

    def is_running(self):
        """Return True while the daemon has not been stopped."""
        with self._lock:
            return self._running

    def rotate(self):
        """Flush pending events before discarding old files.

        Returns {"flushed": True, "lost": 0}.
        """
        with self._lock:
            if self._disk_full:
                return {"flushed": False, "lost": 0}
            self._pending_events = 0
            self._unflushed = 0
            return {"flushed": True, "lost": 0}

    def overflow_count(self):
        """Return the number of recorded backlog-overflow markers."""
        with self._lock:
            return len(self._overflow_records)

    def overflow_records(self):
        """Return the recorded backlog-overflow audit markers."""
        with self._lock:
            return copy.deepcopy(self._overflow_records)

    def _append_journal(self, entry, counts_as_event):
        self._journal.append(copy.deepcopy(entry))
        self._unflushed += 1
        if counts_as_event:
            self._pending_events += 1

    def _queue_full(self):
        if self.queue_size is None:
            return False
        return self._pending_events >= max(int(self.queue_size), 0)

    def _add_exclusion_unlocked(self, path):
        path = self._normalize_path(path)
        if path and path not in self._exclusions:
            self._exclusions.append(path)

    @staticmethod
    def _normalize_path(path):
        if path is None:
            return ""
        value = os.fspath(path)
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        return os.path.normpath(value)

    @staticmethod
    def _is_path_within(path, parent):
        if path == parent:
            return True
        prefix = parent.rstrip(os.sep) + os.sep
        return path.startswith(prefix)

    @classmethod
    def _deduplicate_rules(cls, rules):
        result = []
        seen = set()
        for rule in rules or ():
            rule = copy.deepcopy(rule)
            rule_id = rule.get("id") if isinstance(rule, dict) else None
            if rule_id is not None:
                if rule_id in seen:
                    continue
                seen.add(rule_id)
            result.append(rule)
        return tuple(result)

    @staticmethod
    def _rule_matches(rule, event):
        if not isinstance(rule, dict):
            return False

        matcher = rule.get("match", True)
        if callable(matcher):
            return bool(matcher(event))
        if isinstance(matcher, bool):
            return matcher
        if isinstance(matcher, dict):
            return all(
                key in event and event[key] == value
                for key, value in matcher.items()
            )
        if isinstance(matcher, (list, tuple)):
            return any(
                AuditRuleDispatcher._rule_matches({"match": item}, event)
                for item in matcher
            )

        value = str(matcher).strip().lower()
        if value in ("never", "none", "false", "off", "0"):
            return False
        if value in ("always", "any", "all", "true", "on", "1", "*"):
            return True
        if isinstance(event, dict):
            return str(event.get("type", "")).lower() == value
        return False

    @staticmethod
    def _rule_records(rule):
        if not isinstance(rule, dict):
            return False
        value = rule.get("record")
        if isinstance(value, str):
            return value.strip().lower() not in (
                "",
                "false",
                "no",
                "off",
                "never",
                "0",
            )
        if value is not None:
            return bool(value)
        action = str(rule.get("action", "")).strip().lower()
        return action in ("record", "log", "always")

    @staticmethod
    def _is_security_event(event):
        if not isinstance(event, dict):
            return False
        if event.get("security") is True:
            return True
        event_type = str(event.get("type", "")).lower().replace("-", "_")
        if any(
            token in event_type
            for token in ("auth", "login", "credential", "session")
        ):
            return True
        message = str(event.get("message", "")).lower()
        return "login failed" in message or "authentication failure" in message
