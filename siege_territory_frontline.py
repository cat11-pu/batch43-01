import copy


class SiegeError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SiegeTerritoryFrontline:
    """Stronghold ownership, tick scoring and atomic handover."""

    _WORLD_KILL_SOURCES = {
        "world", "monster", "mob", "npc", "pve", "environment", "ambient"
    }

    def __init__(self, guild_alliances=None, alliances=None,
                 cooldown_seconds=0, max_progress=100):
        self.strongholds = {}
        self.guild_alliances = {}
        self.alliance_guilds = {}
        self.cooldown_seconds = cooldown_seconds
        self.max_progress = max_progress
        self._load_alliances(guild_alliances)
        self._load_alliances(alliances)

    def _load_alliances(self, mapping):
        if not mapping:
            return
        if not isinstance(mapping, dict):
            raise SiegeError("INVALID_ALLIANCES", "alliance mapping must be a dict")
        for key, value in mapping.items():
            if isinstance(value, (list, tuple, set, frozenset)):
                self.set_alliance(key, value)
            else:
                self.set_alliance(value, [key])

    def set_alliance(self, alliance_id, guild_ids):
        if isinstance(guild_ids, str):
            guild_ids = [guild_ids]
        for guild_id in list(guild_ids):
            previous = self.guild_alliances.get(guild_id)
            if previous is not None:
                previous_members = self.alliance_guilds.get(previous, set())
                previous_members.discard(guild_id)
            self.guild_alliances[guild_id] = alliance_id
            self.alliance_guilds.setdefault(alliance_id, set()).add(guild_id)
        return {"alliance_id": alliance_id, "guild_ids": list(guild_ids)}

    def register_alliance(self, alliance_id, guild_ids):
        return self.set_alliance(alliance_id, guild_ids)

    def join_alliance(self, guild_id, alliance_id):
        return self.set_alliance(alliance_id, [guild_id])

    def _require_stronghold(self, stronghold_id):
        try:
            return self.strongholds[stronghold_id]
        except KeyError:
            raise SiegeError("UNKNOWN_STRONGHOLD", stronghold_id) from None

    def register_stronghold(self, stronghold_id, owner=None):
        record = self.strongholds.get(stronghold_id)
        if record is None:
            record = {
                "stronghold_id": stronghold_id,
                "owner": owner,
                "cooldown_until": 0,
                "siege": None,
            }
            self.strongholds[stronghold_id] = record
        elif owner is not None:
            record["owner"] = owner
        return {"stronghold_id": stronghold_id, "owner": record["owner"]}

    @staticmethod
    def _entry_parts(entry):
        if isinstance(entry, str):
            return entry, None
        if isinstance(entry, dict):
            guild_id = (entry.get("guild_id") or entry.get("guild")
                        or entry.get("id"))
            alliance_id = (entry.get("alliance_id") or entry.get("alliance"))
            return guild_id, alliance_id
        if isinstance(entry, (tuple, list)):
            if not entry:
                return None, None
            alliance_id = entry[1] if len(entry) > 1 else None
            return entry[0], alliance_id
        guild_id = getattr(entry, "guild_id", None) or getattr(entry, "id", None)
        alliance_id = getattr(entry, "alliance_id", None)
        return guild_id, alliance_id

    def _normalise_side(self, entries):
        ordered = []
        hints = {}
        for entry in entries or ():
            guild_id, alliance_id = self._entry_parts(entry)
            if guild_id is None:
                raise SiegeError("INVALID_PARTICIPANTS", "guild id is required")
            if guild_id not in ordered:
                ordered.append(guild_id)
            if alliance_id is not None:
                known = self.guild_alliances.get(guild_id)
                if known is not None and known != alliance_id:
                    raise SiegeError("SIDE_CONFLICT", guild_id)
                hints[guild_id] = alliance_id
        return ordered, hints

    def _apply_alliance_hints(self, hints):
        for guild_id, alliance_id in hints.items():
            if self.guild_alliances.get(guild_id) != alliance_id:
                self.set_alliance(alliance_id, [guild_id])

    @staticmethod
    def _alliance_set(guild_ids, guild_alliances):
        return {
            guild_alliances[guild_id]
            for guild_id in guild_ids
            if guild_id in guild_alliances
        }

    def start_siege(self, stronghold_id, attackers, defenders, window_start,
                    window_end, now=None):
        record = self._require_stronghold(stronghold_id)
        current = record.get("siege")
        if current is not None and current.get("status") == "active":
            raise SiegeError("SIEGE_ACTIVE", stronghold_id)
        if window_end <= window_start:
            raise SiegeError("INVALID_WINDOW", stronghold_id)

        start_now = window_start if now is None else now
        if (self.cooldown_seconds > 0
                and record.get("cooldown_until", 0) > start_now):
            raise SiegeError("SIEGE_COOLDOWN", stronghold_id)

        attackers, attacker_hints = self._normalise_side(attackers)
        defenders, defender_hints = self._normalise_side(defenders)
        hints = dict(attacker_hints)
        hints.update(defender_hints)

        owner = record.get("owner")
        if owner is not None:
            if owner in attackers:
                raise SiegeError("SIDE_CONFLICT", owner)
            if owner not in defenders:
                defenders.append(owner)

        if not attackers or not defenders:
            raise SiegeError("INVALID_PARTICIPANTS", "both sides are required")
        if set(attackers) & set(defenders):
            raise SiegeError("SIDE_CONFLICT", sorted(set(attackers) & set(defenders)))

        self._apply_alliance_hints(hints)
        attacker_alliances = self._alliance_set(attackers, self.guild_alliances)
        defender_alliances = self._alliance_set(defenders, self.guild_alliances)
        if attacker_alliances & defender_alliances:
            raise SiegeError(
                "SIDE_CONFLICT",
                sorted(attacker_alliances & defender_alliances),
            )

        participants = attackers + defenders
        record["siege"] = {
            "attackers": attackers,
            "defenders": defenders,
            "window_start": window_start,
            "window_end": window_end,
            "status": "active",
            "started_at": start_now,
            "ended_at": None,
            "last_now": start_now,
            "progress": {guild_id: 0 for guild_id in participants},
            "kills": {guild_id: 0 for guild_id in participants},
            "tick_ids": set(),
            "tick_log": [],
            "kill_ids": set(),
            "kill_log": [],
            "final_snapshot": None,
        }
        return {
            "stronghold_id": stronghold_id,
            "attackers": list(attackers),
            "defenders": list(defenders),
            "window_start": window_start,
            "window_end": window_end,
        }

    def _numeric_damage(self, value):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            if value != value:
                return 0
            return max(0, value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0
        if number != number:
            return 0
        return max(0, number)

    def _cap_progress(self, value):
        if self.max_progress is None:
            return value
        return min(value, self.max_progress)

    @staticmethod
    def _tick_key(tick_id):
        try:
            hash(tick_id)
        except TypeError:
            return repr(tick_id)
        return tick_id

    def _participants(self, siege):
        return siege["attackers"] + siege["defenders"]

    def tick(self, stronghold_id, tick_id, damage_by_guild, now=0):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege")
        if siege is None:
            raise SiegeError("SIEGE_NOT_ACTIVE", stronghold_id)
        if siege["status"] == "closed":
            raise SiegeError("WINDOW_CLOSED", stronghold_id)

        key = self._tick_key(tick_id)
        if key in siege["tick_ids"]:
            return {
                "stronghold_id": stronghold_id,
                "tick_id": tick_id,
                "applied": False,
                "duplicate": True,
                "progress": dict(siege["progress"]),
                "owner": record.get("owner"),
            }

        if now is None:
            now = siege.get("last_now", siege["window_start"])
        if now < siege["window_start"]:
            raise SiegeError("WINDOW_NOT_STARTED", stronghold_id)
        if now > siege["window_end"]:
            raise SiegeError("WINDOW_CLOSED", stronghold_id)

        participants = set(self._participants(siege))
        applied_damage = {}
        ignored = {}
        for guild_id, value in (damage_by_guild or {}).items():
            if guild_id not in participants:
                ignored[guild_id] = "not_a_participant"
                continue
            amount = self._numeric_damage(value)
            if amount <= 0:
                continue
            prior = siege["progress"].get(guild_id, 0)
            updated = self._cap_progress(prior + amount)
            gained = updated - prior
            if gained > 0:
                siege["progress"][guild_id] = updated
                applied_damage[guild_id] = gained

        siege["tick_ids"].add(key)
        siege["tick_log"].append({
            "tick_id": tick_id,
            "damage": applied_damage,
            "now": now,
        })
        siege["last_now"] = max(siege.get("last_now", now), now)
        return {
            "stronghold_id": stronghold_id,
            "tick_id": tick_id,
            "applied": True,
            "duplicate": False,
            "damage": dict(applied_damage),
            "ignored": dict(ignored),
            "progress": dict(siege["progress"]),
            "owner": record.get("owner"),
        }

    def _pick_winner(self, record, siege):
        participants = self._participants(siege)
        progress = siege["progress"]
        kills = siege["kills"]
        owner = record.get("owner")

        highest = max(
            (progress.get(guild_id, 0) for guild_id in participants),
            default=0,
        )
        if highest <= 0:
            return owner

        tied = [
            guild_id for guild_id in participants
            if progress.get(guild_id, 0) == highest
        ]
        highest_kills = max(kills.get(guild_id, 0) for guild_id in tied)
        tied = [
            guild_id for guild_id in tied
            if kills.get(guild_id, 0) == highest_kills
        ]
        if owner in tied:
            return owner
        return tied[0]

    def _snapshot(self, record, siege, now):
        winner = self._pick_winner(record, siege)
        return {
            "stronghold_id": record["stronghold_id"],
            "owner": winner,
            "previous_owner": record.get("owner"),
            "progress": dict(siege["progress"]),
            "kills": dict(siege["kills"]),
            "attackers": list(siege["attackers"]),
            "defenders": list(siege["defenders"]),
            "window_start": siege["window_start"],
            "window_end": siege["window_end"],
            "ended_at": now,
        }

    def end_window(self, stronghold_id, now=0):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege")
        if siege is None:
            raise SiegeError("SIEGE_NOT_ACTIVE", stronghold_id)
        if siege["status"] == "closed":
            snapshot = copy.deepcopy(siege.get("final_snapshot") or {})
            snapshot["already_ended"] = True
            return snapshot
        if now is None:
            now = siege.get("last_now", siege["window_end"])
        if now < siege["window_end"]:
            raise SiegeError("WINDOW_NOT_ENDED", stronghold_id)

        snapshot = self._snapshot(record, siege, now)
        record["owner"] = snapshot["owner"]
        siege["status"] = "closed"
        siege["ended_at"] = now
        siege["last_now"] = max(siege.get("last_now", now), now)
        siege["final_snapshot"] = copy.deepcopy(snapshot)
        if self.cooldown_seconds > 0:
            record["cooldown_until"] = max(
                record.get("cooldown_until", 0),
                now + self.cooldown_seconds,
            )
        else:
            record["cooldown_until"] = 0
        result = copy.deepcopy(snapshot)
        result["already_ended"] = False
        return result

    def settlement(self, stronghold_id):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege")
        if siege is None or siege.get("final_snapshot") is None:
            raise SiegeError("NOT_SETTLED", stronghold_id)
        return copy.deepcopy(siege["final_snapshot"])

    def record_kill(self, stronghold_id, event_id, killer_guild,
                    source="player", now=None):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege")
        if siege is None:
            raise SiegeError("SIEGE_NOT_ACTIVE", stronghold_id)
        if siege["status"] == "closed":
            raise SiegeError("WINDOW_CLOSED", stronghold_id)
        event_now = (
            siege.get("last_now", siege["window_start"])
            if now is None else now
        )
        if event_now < siege["window_start"]:
            raise SiegeError("WINDOW_NOT_STARTED", stronghold_id)
        if event_now > siege["window_end"]:
            raise SiegeError("WINDOW_CLOSED", stronghold_id)

        key = self._tick_key(event_id)
        if key in siege["kill_ids"]:
            return {
                "stronghold_id": stronghold_id,
                "event_id": event_id,
                "applied": False,
                "duplicate": True,
                "kills": dict(siege["kills"]),
            }

        siege["kill_ids"].add(key)
        source_name = str(source).lower()
        world_kill = source_name in self._WORLD_KILL_SOURCES or any(
            marker in source_name
            for marker in ("world", "monster", "mob", "npc", "pve")
        )
        if world_kill:
            return {
                "stronghold_id": stronghold_id,
                "event_id": event_id,
                "applied": False,
                "duplicate": False,
                "ignored": "world_source",
                "kills": dict(siege["kills"]),
            }
        if killer_guild not in set(self._participants(siege)):
            return {
                "stronghold_id": stronghold_id,
                "event_id": event_id,
                "applied": False,
                "duplicate": False,
                "ignored": "not_a_participant",
                "kills": dict(siege["kills"]),
            }

        siege["kills"][killer_guild] = siege["kills"].get(killer_guild, 0) + 1
        siege["kill_log"].append({
            "event_id": event_id,
            "killer_guild": killer_guild,
            "source": source,
            "now": event_now,
        })
        siege["last_now"] = max(siege.get("last_now", event_now), event_now)
        return {
            "stronghold_id": stronghold_id,
            "event_id": event_id,
            "applied": True,
            "duplicate": False,
            "kills": dict(siege["kills"]),
        }

    def respawn_point(self, stronghold_id, guild_id):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege")
        available = guild_id == record.get("owner")
        if (not available and siege is not None
                and siege.get("status") == "active"
                and guild_id in siege["defenders"]):
            available = True
        contested = bool(
            siege is not None
            and siege.get("status") == "active"
            and any(
                member in siege["attackers"] and score > 0
                for member, score in siege["progress"].items()
            )
        )
        return {
            "available": available,
            "stronghold_id": stronghold_id,
            "guild_id": guild_id,
            "contested": contested,
        }

    def buff_affects(self, stronghold_id, guild_id):
        record = self._require_stronghold(stronghold_id)
        owner = record.get("owner")
        if owner is None:
            return False
        if guild_id == owner:
            return True
        owner_alliance = self.guild_alliances.get(owner)
        guild_alliance = self.guild_alliances.get(guild_id)
        return bool(
            owner_alliance is not None
            and owner_alliance == guild_alliance
        )

    def disband_guild(self, guild_id):
        released = []
        for stronghold_id, record in self.strongholds.items():
            siege = record.get("siege")
            if record.get("owner") == guild_id:
                record["owner"] = None
                released.append(stronghold_id)
            if siege is not None:
                for side in ("attackers", "defenders"):
                    if guild_id in siege[side]:
                        siege[side] = [
                            member for member in siege[side]
                            if member != guild_id
                        ]
                if guild_id in siege["progress"]:
                    del siege["progress"][guild_id]
                if guild_id in siege["kills"]:
                    del siege["kills"][guild_id]
                snapshot = siege.get("final_snapshot")
                if snapshot is not None and snapshot.get("owner") == guild_id:
                    snapshot["owner"] = None

        alliance_id = self.guild_alliances.pop(guild_id, None)
        if alliance_id is not None:
            members = self.alliance_guilds.get(alliance_id, set())
            members.discard(guild_id)
            if not members:
                self.alliance_guilds.pop(alliance_id, None)
        return {
            "guild_id": guild_id,
            "released": bool(released),
            "stronghold_ids": released,
        }

    def owner(self, stronghold_id):
        return self._require_stronghold(stronghold_id)["owner"]

    def progress(self, stronghold_id):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege")
        if siege is None:
            return {
                "stronghold_id": stronghold_id,
                "owner": record.get("owner"),
                "progress": {},
                "kills": {},
                "status": None,
            }
        return {
            "stronghold_id": stronghold_id,
            "owner": record.get("owner"),
            "progress": dict(siege["progress"]),
            "kills": dict(siege["kills"]),
            "status": siege["status"],
            "window_start": siege["window_start"],
            "window_end": siege["window_end"],
            "last_now": siege.get("last_now"),
            "remaining_time": self.remaining_time(stronghold_id),
        }

    def remaining_time(self, stronghold_id, now=None):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege")
        if siege is None:
            return 0
        if siege.get("status") == "closed":
            return 0
        current = (
            siege.get("last_now", siege["window_start"])
            if now is None else now
        )
        return max(0, siege["window_end"] - current)

    def cooldown_remaining(self, stronghold_id, now=None):
        record = self._require_stronghold(stronghold_id)
        siege = record.get("siege") or {}
        current = siege.get("last_now", 0) if now is None else now
        return max(0, record.get("cooldown_until", 0) - current)

    def dump(self):
        strongholds = {}
        for stronghold_id, record in self.strongholds.items():
            siege = record.get("siege")
            encoded_siege = None
            if siege is not None:
                encoded_siege = {
                    "attackers": list(siege["attackers"]),
                    "defenders": list(siege["defenders"]),
                    "window_start": siege["window_start"],
                    "window_end": siege["window_end"],
                    "status": siege["status"],
                    "started_at": siege.get("started_at"),
                    "ended_at": siege.get("ended_at"),
                    "last_now": siege.get("last_now"),
                    "progress": dict(siege["progress"]),
                    "kills": dict(siege["kills"]),
                    "tick_ids": list(siege["tick_ids"]),
                    "tick_log": copy.deepcopy(siege["tick_log"]),
                    "kill_ids": list(siege["kill_ids"]),
                    "kill_log": copy.deepcopy(siege["kill_log"]),
                    "final_snapshot": copy.deepcopy(
                        siege.get("final_snapshot")
                    ),
                }
            strongholds[stronghold_id] = {
                "stronghold_id": stronghold_id,
                "owner": record.get("owner"),
                "cooldown_until": record.get("cooldown_until", 0),
                "siege": encoded_siege,
            }
        return {
            "version": 1,
            "cooldown_seconds": self.cooldown_seconds,
            "max_progress": self.max_progress,
            "guild_alliances": dict(self.guild_alliances),
            "strongholds": strongholds,
        }

    def load(self, state):
        if not isinstance(state, dict):
            raise SiegeError("INVALID_STATE", "state must be a dict")
        raw_strongholds = state.get("strongholds")
        if not isinstance(raw_strongholds, dict):
            raise SiegeError("INVALID_STATE", "strongholds are required")

        cooldown_seconds = state.get("cooldown_seconds", self.cooldown_seconds)
        max_progress = state.get("max_progress", self.max_progress)
        guild_alliances = {}
        alliance_guilds = {}
        for guild_id, alliance_id in (state.get("guild_alliances") or {}).items():
            guild_alliances[guild_id] = alliance_id
            alliance_guilds.setdefault(alliance_id, set()).add(guild_id)

        strongholds = {}
        for stronghold_id, raw in raw_strongholds.items():
            if not isinstance(raw, dict):
                raise SiegeError("INVALID_STATE", stronghold_id)
            record = {
                "stronghold_id": stronghold_id,
                "owner": raw.get("owner"),
                "cooldown_until": raw.get("cooldown_until", 0),
                "siege": None,
            }
            raw_siege = raw.get("siege")
            if raw_siege is not None:
                if not isinstance(raw_siege, dict):
                    raise SiegeError("INVALID_STATE", stronghold_id)
                siege = {
                    "attackers": list(raw_siege.get("attackers") or []),
                    "defenders": list(raw_siege.get("defenders") or []),
                    "window_start": raw_siege.get("window_start", 0),
                    "window_end": raw_siege.get("window_end", 0),
                    "status": raw_siege.get("status", "active"),
                    "started_at": raw_siege.get(
                        "started_at", raw_siege.get("window_start", 0)
                    ),
                    "ended_at": raw_siege.get("ended_at"),
                    "last_now": raw_siege.get(
                        "last_now", raw_siege.get("window_start", 0)
                    ),
                    "progress": dict(raw_siege.get("progress") or {}),
                    "kills": dict(raw_siege.get("kills") or {}),
                    "tick_ids": set(raw_siege.get("tick_ids") or []),
                    "tick_log": copy.deepcopy(raw_siege.get("tick_log") or []),
                    "kill_ids": set(raw_siege.get("kill_ids") or []),
                    "kill_log": copy.deepcopy(raw_siege.get("kill_log") or []),
                    "final_snapshot": copy.deepcopy(
                        raw_siege.get("final_snapshot")
                    ),
                }
                participants = siege["attackers"] + siege["defenders"]
                for guild_id in participants:
                    siege["progress"].setdefault(guild_id, 0)
                    siege["kills"].setdefault(guild_id, 0)
                if siege["tick_log"]:
                    rebuilt = {guild_id: 0 for guild_id in participants}
                    seen = set()
                    for entry in siege["tick_log"]:
                        key = self._tick_key(entry.get("tick_id"))
                        if key in seen:
                            continue
                        seen.add(key)
                        siege["tick_ids"].add(key)
                        for guild_id, amount in (entry.get("damage") or {}).items():
                            if guild_id not in rebuilt:
                                continue
                            prior = rebuilt[guild_id]
                            updated = prior + max(0, amount)
                            if max_progress is not None:
                                updated = min(updated, max_progress)
                            rebuilt[guild_id] = updated
                    siege["progress"] = rebuilt
                if siege["kill_log"]:
                    rebuilt_kills = {guild_id: 0 for guild_id in participants}
                    for entry in siege["kill_log"]:
                        killer = entry.get("killer_guild")
                        if killer in rebuilt_kills:
                            rebuilt_kills[killer] += 1
                    siege["kills"] = rebuilt_kills
                record["siege"] = siege
            strongholds[stronghold_id] = record

        self.cooldown_seconds = cooldown_seconds
        self.max_progress = max_progress
        self.guild_alliances = guild_alliances
        self.alliance_guilds = alliance_guilds
        self.strongholds = strongholds
        return {"strongholds": len(strongholds)}
