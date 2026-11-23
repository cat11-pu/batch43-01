import copy
import json
import threading


class RaidError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RaidSignupWaitlist:
    """Raid roster, role quotas and ordered waitlist."""

    def __init__(self, capacity=None):
        default = {"tank": 1, "healer": 1, "dps": 3}
        if capacity is not None:
            default = self._normalise_capacity(capacity, "INVALID_CAPACITY")
        self.default_capacity = default
        self._cycles = {}
        self._lock = threading.RLock()

    def open_cycle(self, raid_id, capacity, lock_at, now=0):
        with self._lock:
            capacity = self.default_capacity if capacity is None else capacity
            capacity = self._normalise_capacity(capacity, "INVALID_CAPACITY")
            if lock_at is None:
                raise RaidError("INVALID_LOCK_TIME")

            previous_audit = []
            if raid_id in self._cycles:
                previous_audit = copy.deepcopy(self._cycles[raid_id]["audit"])
            cycle = {
                "raid_id": raid_id,
                "capacity": capacity,
                "lock_at": lock_at,
                "locked": False,
                "locked_at": None,
                "cancelled": False,
                "combat_started": False,
                "started_at": None,
                "roster": [],
                "waitlist": [],
                "instance": [],
                "bosses": {},
                "audit": previous_audit,
                "next_seq": 0,
                "version": 0,
                "awaiting_waitlist_refresh": False,
            }
            self._cycles[raid_id] = cycle
            self._audit(cycle, "cycle_opened", now, lock_at=lock_at)
            return self.snapshot(raid_id)

    def signup(self, raid_id, character_id, role, account_id=None, now=0):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            self._ensure_not_started(cycle)
            if self._is_locked(cycle, now):
                raise RaidError("SIGNUP_LOCKED", {"raid_id": raid_id})
            self._ensure_role(cycle, role)

            entries = cycle["roster"] + cycle["waitlist"]
            if any(row["character_id"] == character_id for row in entries):
                raise RaidError(
                    "DUPLICATE_CHARACTER",
                    {"raid_id": raid_id, "character_id": character_id},
                )
            if account_id is not None and any(
                row["account_id"] == account_id for row in entries
            ):
                raise RaidError(
                    "DUPLICATE_ACCOUNT",
                    {"raid_id": raid_id, "account_id": account_id},
                )

            entry = self._new_entry(
                cycle, character_id, role, account_id, now
            )
            if self._has_slot(cycle, role):
                cycle["roster"].append(entry)
                status = "roster"
            else:
                cycle["waitlist"].append(entry)
                status = "waitlist"

            cycle["awaiting_waitlist_refresh"] = False
            self._audit(
                cycle,
                "signup",
                now,
                character_id=character_id,
                account_id=account_id,
                role=role,
                status=status,
            )
            self._finish_mutation(cycle)
            return self._result_for(cycle, entry, status)

    def roster(self, raid_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            return self._rows(cycle["roster"], "roster")

    def waitlist(self, raid_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            rows = self._rows(self._sorted(cycle["waitlist"]), "waitlist")
            cycle["awaiting_waitlist_refresh"] = False
            return rows

    def promote_next(self, raid_id, role=None):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            self._ensure_not_started(cycle)
            if cycle["locked"]:
                raise RaidError("ROSTER_LOCKED", {"raid_id": raid_id})
            if role is not None:
                self._ensure_role(cycle, role)

            candidates = self._sorted(cycle["waitlist"])
            if role is not None:
                candidates = [
                    row for row in candidates if row["role"] == role
                ]

            if not candidates:
                if cycle["awaiting_waitlist_refresh"]:
                    raise RaidError("NO_SLOT", {"raid_id": raid_id, "role": role})
                return {"promoted": None, "notified": False}

            entry = candidates[0]
            if not self._has_slot(cycle, entry["role"]):
                if cycle["awaiting_waitlist_refresh"]:
                    raise RaidError(
                        "NO_SLOT",
                        {"raid_id": raid_id, "role": entry["role"]},
                    )
                self._replace_last_roster_member(
                    cycle, entry["role"], entry["signed_at"]
                )
            if not self._has_slot(cycle, entry["role"]):
                raise RaidError(
                    "NO_SLOT",
                    {"raid_id": raid_id, "role": entry["role"]},
                )

            cycle["waitlist"].remove(entry)
            cycle["roster"].append(entry)
            cycle["awaiting_waitlist_refresh"] = True
            self._audit(
                cycle,
                "promoted",
                entry["signed_at"],
                character_id=entry["character_id"],
                role=entry["role"],
            )
            self._audit(
                cycle,
                "notified",
                entry["signed_at"],
                character_id=entry["character_id"],
                role=entry["role"],
            )
            self._finish_mutation(cycle)
            return {
                "promoted": entry["character_id"],
                "notified": True,
                "role": entry["role"],
            }

    def change_role(self, raid_id, character_id, role):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            self._ensure_not_started(cycle)
            if cycle["locked"]:
                raise RaidError("ROSTER_LOCKED", {"raid_id": raid_id})
            self._ensure_role(cycle, role)

            entry, status = self._find(cycle, character_id)
            previous_role = entry["role"]
            if previous_role == role:
                return self._result_for(cycle, entry, status)

            if status == "roster" and not self._has_slot(cycle, role):
                raise RaidError(
                    "NO_SLOT",
                    {"raid_id": raid_id, "role": role},
                )

            entry["role"] = role
            cycle["awaiting_waitlist_refresh"] = False
            self._audit(
                cycle,
                "role_changed",
                entry["signed_at"],
                character_id=character_id,
                previous_role=previous_role,
                role=role,
            )
            self._finish_mutation(cycle)
            return self._result_for(cycle, entry, status)

    def leave(self, raid_id, character_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            if cycle["locked"] and not cycle["combat_started"]:
                raise RaidError("ROSTER_LOCKED", {"raid_id": raid_id})

            entry, status = self._find(cycle, character_id)
            self._remove_entry(cycle, entry, status)
            cycle["awaiting_waitlist_refresh"] = False
            self._audit(
                cycle,
                "left",
                entry["signed_at"],
                character_id=character_id,
                status=status,
            )
            self._finish_mutation(cycle)
            return {
                "left": True,
                "character_id": character_id,
                "status": status,
            }

    def lock(self, raid_id, now=0):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            cycle["locked"] = True
            if cycle["locked_at"] is None:
                cycle["locked_at"] = now
            self._audit(cycle, "locked", now, lock_at=cycle["lock_at"])
            self._finish_mutation(cycle)
            return {
                "locked": True,
                "lock_at": cycle["lock_at"],
                "locked_at": cycle["locked_at"],
            }

    def is_locked(self, raid_id, now=0):
        with self._lock:
            cycle = self._cycle(raid_id)
            return self._is_locked(cycle, now)

    def join_raid(self, raid_id, character_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            if cycle["combat_started"]:
                raise RaidError("RAID_STARTED", {"raid_id": raid_id})

            entry, status = self._find(cycle, character_id)
            if status != "roster":
                raise RaidError(
                    "NO_SLOT",
                    {"raid_id": raid_id, "character_id": character_id},
                )
            if not self._has_slot(cycle, entry["role"]):
                raise RaidError(
                    "NO_SLOT",
                    {"raid_id": raid_id, "role": entry["role"]},
                )

            entry["joined"] = True
            self._audit(
                cycle,
                "joined_instance",
                entry["signed_at"],
                character_id=character_id,
                role=entry["role"],
            )
            self._finish_mutation(cycle)
            return {
                "joined": True,
                "character_id": character_id,
                "role": entry["role"],
            }

    def record_kill(self, raid_id, boss_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            cycle["combat_started"] = True
            if cycle["started_at"] is None:
                cycle["started_at"] = 0

            for entry in cycle["roster"]:
                character_id = entry["character_id"]
                bosses = cycle["bosses"].setdefault(character_id, [])
                if boss_id not in bosses:
                    bosses.append(boss_id)

            self._audit(cycle, "boss_recorded", 0, boss_id=boss_id)
            self._finish_mutation(cycle)
            return {
                "boss_id": boss_id,
                "progress": {
                    character_id: len(bosses)
                    for character_id, bosses in cycle["bosses"].items()
                },
            }

    def abandon(self, raid_id, character_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            self._ensure_open(cycle)
            try:
                entry, status = self._find(cycle, character_id)
            except RaidError:
                return {
                    "character_id": character_id,
                    "removed": False,
                    "deserter": False,
                }

            deserter = status == "roster"
            self._remove_entry(cycle, entry, status)
            cycle["awaiting_waitlist_refresh"] = False
            self._audit(
                cycle,
                "abandoned",
                entry["signed_at"],
                character_id=character_id,
                status=status,
                deserter=deserter,
            )
            self._finish_mutation(cycle)
            return {
                "character_id": character_id,
                "removed": True,
                "status": status,
                "deserter": deserter,
            }

    def cancel_raid(self, raid_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            roster_count = len(cycle["roster"])
            waitlist_count = len(cycle["waitlist"])

            cycle["roster"] = []
            cycle["waitlist"] = []
            cycle["instance"] = []
            cycle["bosses"] = {}
            cycle["cancelled"] = True
            cycle["combat_started"] = False
            cycle["awaiting_waitlist_refresh"] = False
            self._audit(
                cycle,
                "cancelled",
                0,
                roster_count=roster_count,
                waitlist_count=waitlist_count,
            )
            cycle["version"] += 1
            return {
                "cancelled": True,
                "cleared_roster": roster_count,
                "cleared_waitlist": waitlist_count,
            }

    def copy_roster(self, source_raid, target_raid, valid_characters):
        with self._lock:
            source = self._cycle(source_raid)
            target = self._cycle(target_raid)
            self._ensure_open(target)
            self._ensure_not_started(target)
            if target["locked"]:
                raise RaidError("ROSTER_LOCKED", {"raid_id": target_raid})

            valid = self._valid_character_set(valid_characters)
            copied = []
            skipped = []
            ordered = self._sorted(source["roster"]) + self._sorted(
                source["waitlist"]
            )

            for original in ordered:
                character_id = original["character_id"]
                if valid is not None and character_id not in valid:
                    skipped.append(character_id)
                    continue
                if self._contains_character(target, character_id):
                    skipped.append(character_id)
                    continue
                if original["account_id"] is not None and self._contains_account(
                    target, original["account_id"]
                ):
                    skipped.append(character_id)
                    continue

                entry = self._new_entry(
                    target,
                    character_id,
                    original["role"],
                    original["account_id"],
                    original["signed_at"],
                )
                if self._has_slot(target, entry["role"]):
                    target["roster"].append(entry)
                else:
                    target["waitlist"].append(entry)
                copied.append(character_id)

            target["awaiting_waitlist_refresh"] = False
            self._audit(
                target,
                "roster_copied",
                0,
                source_raid=source_raid,
                copied=copied,
                skipped=skipped,
            )
            self._finish_mutation(target)
            return {
                "copied": copied,
                "skipped": skipped,
                "roster_count": len(target["roster"]),
                "waitlist_count": len(target["waitlist"]),
            }

    def snapshot(self, raid_id):
        with self._lock:
            cycle = self._cycle(raid_id)
            return {
                "raid_id": cycle["raid_id"],
                "capacity": copy.deepcopy(cycle["capacity"]),
                "lock_at": cycle["lock_at"],
                "locked": cycle["locked"],
                "locked_at": cycle["locked_at"],
                "cancelled": cycle["cancelled"],
                "combat_started": cycle["combat_started"],
                "roster": self._rows(cycle["roster"], "roster"),
                "waitlist": self._rows(
                    self._sorted(cycle["waitlist"]), "waitlist"
                ),
                "instance": copy.deepcopy(cycle["instance"]),
                "progress": {
                    character_id: len(bosses)
                    for character_id, bosses in cycle["bosses"].items()
                },
                "audit": copy.deepcopy(cycle["audit"]),
                "version": cycle["version"],
            }

    def dump(self):
        with self._lock:
            return {
                "default_capacity": copy.deepcopy(self.default_capacity),
                "cycles": copy.deepcopy(self._cycles),
            }

    def load(self, state):
        with self._lock:
            if isinstance(state, str):
                state = json.loads(state)
            if not isinstance(state, dict):
                raise RaidError("INVALID_STATE")
            if "cycles" in state:
                cycles = state["cycles"]
                default_capacity = state.get("default_capacity")
            else:
                cycles = state
                default_capacity = None
            if not isinstance(cycles, dict):
                raise RaidError("INVALID_STATE")

            restored = {}
            for raid_id, raw_cycle in cycles.items():
                restored[raid_id] = self._normalise_cycle(raid_id, raw_cycle)
            self._cycles = restored
            if default_capacity is not None:
                self.default_capacity = self._normalise_capacity(
                    default_capacity, "INVALID_STATE"
                )
            return self

    @staticmethod
    def _normalise_capacity(capacity, code):
        if not isinstance(capacity, dict):
            raise RaidError(code, {"capacity": capacity})
        result = {}
        for role, slots in capacity.items():
            if not isinstance(role, str) or isinstance(slots, bool):
                raise RaidError(code, {"capacity": capacity})
            try:
                slots = int(slots)
            except (TypeError, ValueError):
                raise RaidError(code, {"capacity": capacity}) from None
            if slots < 0:
                raise RaidError(code, {"capacity": capacity})
            result[role] = slots
        return result

    @staticmethod
    def _new_entry(cycle, character_id, role, account_id, now):
        seq = cycle["next_seq"]
        cycle["next_seq"] += 1
        return {
            "character_id": character_id,
            "role": role,
            "account_id": account_id,
            "signed_at": now,
            "signup_seq": seq,
            "joined": False,
        }

    @staticmethod
    def _sorted(entries):
        return sorted(
            entries,
            key=lambda row: (
                row["signed_at"],
                str(row["character_id"]),
                row["signup_seq"],
            ),
        )

    def _rows(self, entries, status):
        rows = []
        for position, entry in enumerate(self._sorted(entries), start=1):
            row = copy.deepcopy(entry)
            row["status"] = status
            row["position"] = position if status == "waitlist" else None
            rows.append(row)
        return rows

    def _result_for(self, cycle, entry, status):
        position = None
        if status == "waitlist":
            ordered = self._sorted(cycle["waitlist"])
            position = ordered.index(entry) + 1
        return {
            "character_id": entry["character_id"],
            "role": entry["role"],
            "status": status,
            "position": position,
            "waitlisted": status == "waitlist",
        }

    @staticmethod
    def _contains_character(cycle, character_id):
        return any(
            row["character_id"] == character_id
            for row in cycle["roster"] + cycle["waitlist"]
        )

    @staticmethod
    def _contains_account(cycle, account_id):
        return any(
            row["account_id"] == account_id
            for row in cycle["roster"] + cycle["waitlist"]
        )

    @staticmethod
    def _find(cycle, character_id):
        for row in cycle["roster"]:
            if row["character_id"] == character_id:
                return row, "roster"
        for row in cycle["waitlist"]:
            if row["character_id"] == character_id:
                return row, "waitlist"
        raise RaidError("NOT_SIGNED_UP", {"character_id": character_id})

    @staticmethod
    def _remove_entry(cycle, entry, status):
        if status == "roster":
            cycle["roster"].remove(entry)
        else:
            cycle["waitlist"].remove(entry)

    def _replace_last_roster_member(self, cycle, role, signed_at):
        members = [
            row for row in self._sorted(cycle["roster"]) if row["role"] == role
        ]
        if not members:
            return
        victim = members[-1]
        cycle["roster"].remove(victim)
        self._audit(
            cycle,
            "roster_evicted",
            signed_at,
            character_id=victim["character_id"],
            role=victim["role"],
        )

    def _has_slot(self, cycle, role):
        used = sum(1 for row in cycle["roster"] if row["role"] == role)
        return used < cycle["capacity"].get(role, 0)

    @staticmethod
    def _ensure_role(cycle, role):
        if role not in cycle["capacity"]:
            raise RaidError("UNKNOWN_ROLE", {"role": role})

    @staticmethod
    def _ensure_open(cycle):
        if cycle["cancelled"]:
            raise RaidError("RAID_CANCELLED", {"raid_id": cycle["raid_id"]})

    @staticmethod
    def _ensure_not_started(cycle):
        if cycle["combat_started"]:
            raise RaidError("RAID_STARTED", {"raid_id": cycle["raid_id"]})

    def _is_locked(self, cycle, now):
        if cycle["locked"]:
            return True
        if now >= cycle["lock_at"]:
            cycle["locked"] = True
            if cycle["locked_at"] is None:
                cycle["locked_at"] = cycle["lock_at"]
            return True
        return False

    def _sync_instance(self, cycle):
        cycle["instance"] = [
            {
                "character_id": row["character_id"],
                "role": row["role"],
                "joined": row["joined"],
            }
            for row in self._sorted(cycle["roster"])
        ]

    def _finish_mutation(self, cycle):
        cycle["version"] += 1
        self._sync_instance(cycle)
        self._assert_invariants(cycle)

    @staticmethod
    def _assert_invariants(cycle):
        roster = cycle["roster"]
        waitlist = cycle["waitlist"]
        if len({row["character_id"] for row in roster}) != len(roster):
            raise RaidError("INVARIANT_VIOLATION", {"reason": "duplicate_roster"})
        if len({row["character_id"] for row in waitlist}) != len(waitlist):
            raise RaidError(
                "INVARIANT_VIOLATION", {"reason": "duplicate_waitlist"}
            )
        roster_ids = {row["character_id"] for row in roster}
        waitlist_ids = {row["character_id"] for row in waitlist}
        if roster_ids & waitlist_ids:
            raise RaidError(
                "INVARIANT_VIOLATION", {"reason": "roster_waitlist_overlap"}
            )
        for role, capacity in cycle["capacity"].items():
            if sum(1 for row in roster if row["role"] == role) > capacity:
                raise RaidError(
                    "INVARIANT_VIOLATION", {"reason": "role_over_capacity"}
                )
        for row in roster + waitlist:
            if row["role"] not in cycle["capacity"]:
                raise RaidError(
                    "INVARIANT_VIOLATION", {"reason": "unknown_role"}
                )

    def _cycle(self, raid_id):
        try:
            return self._cycles[raid_id]
        except KeyError:
            raise RaidError("NO_CYCLE", {"raid_id": raid_id}) from None

    @staticmethod
    def _audit(cycle, event, now=0, **details):
        row = {"event": event, "at": now}
        row.update(details)
        cycle["audit"].append(row)

    @staticmethod
    def _valid_character_set(valid_characters):
        if valid_characters is None:
            return None
        if isinstance(valid_characters, dict):
            return set(valid_characters)
        return set(valid_characters)

    def _normalise_cycle(self, raid_id, raw_cycle):
        if not isinstance(raw_cycle, dict):
            raise RaidError("INVALID_STATE", {"raid_id": raid_id})
        cycle = copy.deepcopy(raw_cycle)
        cycle["raid_id"] = cycle.get("raid_id", raid_id)
        cycle["capacity"] = self._normalise_capacity(
            cycle["capacity"], "INVALID_STATE"
        )
        cycle.setdefault("lock_at", 0)
        cycle.setdefault("locked", False)
        cycle.setdefault("locked_at", None)
        cycle.setdefault("cancelled", False)
        cycle.setdefault("combat_started", False)
        cycle.setdefault("started_at", None)
        cycle.setdefault("roster", [])
        cycle.setdefault("waitlist", [])
        cycle.setdefault("instance", [])
        cycle.setdefault("bosses", {})
        cycle.setdefault("audit", [])
        cycle.setdefault("next_seq", 0)
        cycle.setdefault("version", 0)
        cycle.setdefault("awaiting_waitlist_refresh", False)
        self._sync_instance(cycle)
        self._assert_invariants(cycle)
        return cycle
