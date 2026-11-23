from copy import deepcopy


class QuestError(ValueError):
    """Raised when a quest operation violates chain or inventory rules."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class QuestChainPrereq:
    """Quest prerequisite graph, per-character instances and rewards."""

    def __init__(self):
        self._quests = {}
        self._aliases = {}
        self._factions = {}
        self._instances = {}
        self._active = {}
        self._completed = {}
        self._latest = {}
        self._inventory = {}
        self._mailbox = {}
        self._next_instance = 0
        self._accept_seq = 0

    # ------------------------------------------------------------------
    # Definitions and graph
    # ------------------------------------------------------------------
    def define_quest(self, quest_id, prereqs=None, faction=None, objective=None,
                     items_required=None, rewards=None, grant_on_accept=None,
                     aliases=None):
        canonical = self._resolve(quest_id)

        if isinstance(prereqs, str):
            prereqs = [prereqs]
        if isinstance(aliases, str):
            aliases = [aliases]

        existing = self._quests.get(canonical)
        existing_aliases = list(existing["aliases"]) if existing else []
        for alias in aliases or []:
            if alias == canonical:
                continue
            owner = self._aliases.get(alias, alias if alias in self._quests else None)
            if owner is not None and owner != canonical:
                raise QuestError("alias_conflict", {
                    "alias": alias,
                    "existing_quest": owner,
                    "quest_id": canonical,
                })
            if alias not in existing_aliases:
                existing_aliases.append(alias)
            self._aliases[alias] = canonical

        self._quests[canonical] = {
            "quest_id": canonical,
            "prereqs": list(prereqs or []),
            "faction": self._normalise_faction(faction),
            "objective": self._count_map(objective),
            "items_required": self._count_map(items_required),
            "rewards": dict(rewards or {}),
            "grant_on_accept": self._count_map(grant_on_accept),
            "aliases": existing_aliases,
        }
        self._aliases[canonical] = canonical
        return deepcopy(self._quests[canonical])

    def set_faction(self, character, faction):
        self._ensure_character(character)
        self._factions[character] = self._normalise_faction(faction)
        return {"character": character, "faction": self._factions[character]}

    # ------------------------------------------------------------------
    # Quest lifecycle
    # ------------------------------------------------------------------
    def accept(self, character, quest_id, now=0):
        canonical = self._resolve(quest_id)
        self._ensure_character(character)

        active_id = self._active.get(character, {}).get(canonical)
        if active_id is not None:
            instance = self._instances[active_id]
            if instance["status"] == "active":
                return {
                    "status": "already_active",
                    "quest_id": canonical,
                    "instance_id": active_id,
                }

        if self._is_completed(character, canonical):
            return {
                "status": "already_completed",
                "quest_id": canonical,
                "instance_id": self._latest.get(character, {}).get(canonical),
            }

        definition = self._quests.get(canonical)
        if definition is None:
            raise QuestError("unknown_quest", {"quest_id": quest_id})

        required_faction = definition["faction"]
        actual_faction = self._factions.get(character)
        if required_faction is not None and actual_faction != required_faction:
            raise QuestError("faction_locked", {
                "quest_id": canonical,
                "required": required_faction,
                "actual": actual_faction,
            })

        missing = self._missing_prerequisites(character, canonical)
        if missing:
            raise QuestError("prereq_incomplete", {
                "quest_id": canonical,
                "missing": missing,
            })

        self._next_instance += 1
        self._accept_seq += 1
        instance_id = "quest-instance-{}".format(self._next_instance)
        instance = {
            "instance_id": instance_id,
            "character": character,
            "quest_id": canonical,
            "status": "active",
            "counters": {name: 0 for name in definition["objective"]},
            "item_ledger": {},
            "accepted_at": now,
            "updated_at": now,
            "completed_at": None,
            "ended_at": None,
            "accept_seq": self._accept_seq,
            "reward_mailed": False,
        }

        grant = definition["grant_on_accept"]
        if grant:
            for item_id, count in grant.items():
                self._add_item(character, item_id, count)
                instance["item_ledger"][item_id] = (
                    instance["item_ledger"].get(item_id, 0) + count
                )

        self._instances[instance_id] = instance
        self._active.setdefault(character, {})[canonical] = instance_id
        self._latest.setdefault(character, {})[canonical] = instance_id
        return {
            "status": "active",
            "quest_id": canonical,
            "instance_id": instance_id,
            "counters": dict(instance["counters"]),
        }

    def progress(self, character, quest_id, amount=1, objective=None,
                 party=None, now=0):
        canonical = self._resolve(quest_id)
        self._ensure_character(character)
        instance = self._active_instance(character, canonical)
        if instance is None:
            raise QuestError("not_active", {"quest_id": canonical})

        definition = self._quests.get(canonical)
        if definition is None:
            raise QuestError("unknown_quest", {"quest_id": quest_id})

        objectives = definition["objective"]
        if objective is None:
            if len(objectives) == 1:
                objective = next(iter(objectives))
            elif not objectives:
                raise QuestError("unknown_objective", {"quest_id": canonical})
            else:
                raise QuestError("objective_required", {"quest_id": canonical})
        if objective not in objectives:
            raise QuestError("unknown_objective", {
                "quest_id": canonical,
                "objective": objective,
            })

        try:
            amount = int(amount)
        except (TypeError, ValueError):
            raise QuestError("invalid_amount", {"amount": amount})
        if amount <= 0:
            raise QuestError("invalid_amount", {"amount": amount})

        recipients = []
        for member in [character] + list(party or []):
            if member not in recipients:
                recipients.append(member)

        credited = []
        for member in recipients:
            member_instance = self._active_instance(member, canonical)
            if member_instance is None:
                continue
            member_instance["counters"][objective] = (
                member_instance["counters"].get(objective, 0) + amount
            )
            member_instance["updated_at"] = now
            credited.append(member)

        return {
            "status": "progressed",
            "quest_id": canonical,
            "objective": objective,
            "amount": amount,
            "credited": credited,
        }

    def complete(self, character, quest_id, now=0):
        canonical = self._resolve(quest_id)
        self._ensure_character(character)

        instance = self._active_instance(character, canonical)
        if instance is None:
            if self._is_completed(character, canonical):
                return {
                    "status": "already_completed",
                    "quest_id": canonical,
                    "instance_id": self._latest.get(character, {}).get(canonical),
                }
            raise QuestError("not_active", {"quest_id": canonical})

        definition = self._quests.get(canonical)
        if definition is None:
            raise QuestError("unknown_quest", {"quest_id": quest_id})

        missing_items = {}
        inventory = self._inventory.get(character, {})
        for item_id, required in definition["items_required"].items():
            held = inventory.get(item_id, 0)
            if held < required:
                missing_items[item_id] = required - held

        missing_objectives = {}
        for name, target in definition["objective"].items():
            held = instance["counters"].get(name, 0)
            if held < target:
                missing_objectives[name] = target - held

        if missing_items or missing_objectives:
            raise QuestError("requirements_unmet", {
                "quest_id": canonical,
                "items": missing_items,
                "objectives": missing_objectives,
            })

        # Validate everything before mutating so the status change, item
        # consumption and reward mail are committed as one operation.
        for item_id, required in definition["items_required"].items():
            self._remove_item(character, item_id, required)
            removed_from_ledger = min(
                instance["item_ledger"].get(item_id, 0), required
            )
            if removed_from_ledger:
                instance["item_ledger"][item_id] -= removed_from_ledger
                if instance["item_ledger"][item_id] <= 0:
                    del instance["item_ledger"][item_id]

        instance["status"] = "completed"
        instance["completed_at"] = now
        instance["updated_at"] = now
        self._active.get(character, {}).pop(canonical, None)
        self._completed.setdefault(character, set()).add(canonical)
        self._latest.setdefault(character, {})[canonical] = instance["instance_id"]

        rewards = dict(definition["rewards"])
        if rewards and not instance["reward_mailed"]:
            mail = {
                "quest_id": canonical,
                "instance_id": instance["instance_id"],
                "delivered_at": now,
            }
            mail.update(rewards)
            self._mailbox.setdefault(character, []).append(mail)
            instance["reward_mailed"] = True

        return {
            "status": "completed",
            "quest_id": canonical,
            "instance_id": instance["instance_id"],
            "rewards": rewards,
        }

    def abandon(self, character, quest_id, now=0):
        return self._end_active_quest(character, quest_id, "abandoned", now)

    def fail(self, character, quest_id, now=0):
        return self._end_active_quest(character, quest_id, "failed", now)

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    def add_item(self, character, item_id, count=1):
        self._ensure_character(character)
        count = self._positive_count(count)
        self._add_item(character, item_id, count)

        # Items gathered while a quest is active belong to that quest and are
        # recovered verbatim if the quest is abandoned or failed.
        for canonical in list(self._active.get(character, {})):
            definition = self._quests.get(canonical)
            if definition and item_id in definition["items_required"]:
                instance = self._active_instance(character, canonical)
                if instance is not None:
                    instance["item_ledger"][item_id] = (
                        instance["item_ledger"].get(item_id, 0) + count
                    )

        return {"status": "added", "item_id": item_id, "count": count}

    def transfer_item(self, character, target_character, item_id, count=1):
        count = self._positive_count(count)
        if item_id in self._bound_item_ids():
            raise QuestError("item_bound", {"item_id": item_id})

        source_inventory = self._inventory.get(character, {})
        if source_inventory.get(item_id, 0) < count:
            raise QuestError("insufficient_items", {
                "item_id": item_id,
                "required": count,
                "held": source_inventory.get(item_id, 0),
            })

        self._ensure_character(target_character)
        self._remove_item(character, item_id, count)
        self._add_item(target_character, item_id, count)
        return {
            "status": "transferred",
            "item_id": item_id,
            "count": count,
            "from": character,
            "to": target_character,
        }

    def inventory(self, character):
        return dict(self._inventory.get(character, {}))

    # ------------------------------------------------------------------
    # Read-side views
    # ------------------------------------------------------------------
    def quest_state(self, character, quest_id):
        canonical = self._resolve(quest_id)
        instance_id = self._latest.get(character, {}).get(canonical)
        instance = self._instances.get(instance_id) if instance_id else None

        if self._is_completed(character, canonical):
            status = "completed"
        elif instance is not None:
            status = instance["status"]
        else:
            status = "not_started"

        state = {
            "quest_id": canonical,
            "status": status,
            "instance_id": instance_id,
            "counters": {},
        }
        if instance is not None:
            state["counters"] = dict(instance["counters"])
            state["accepted_at"] = instance["accepted_at"]
            state["updated_at"] = instance["updated_at"]
        return state

    def tracker(self, character):
        rows = []
        active = self._active.get(character, {})
        for canonical, instance_id in active.items():
            instance = self._instances.get(instance_id)
            if instance is None or instance["status"] != "active":
                continue
            definition = self._quests.get(canonical, {})
            rows.append({
                "quest_id": canonical,
                "instance_id": instance_id,
                "status": "active",
                "counters": dict(instance["counters"]),
                "objectives": dict(definition.get("objective", {})),
                "accepted_at": instance["accepted_at"],
            })
        return rows

    def mailbox(self, character):
        return deepcopy(self._mailbox.get(character, []))

    def import_completion(self, character, quest_ids):
        if isinstance(quest_ids, str):
            quest_ids = [quest_ids]
        self._ensure_character(character)

        imported = []
        for quest_id in quest_ids or []:
            canonical = self._resolve(quest_id)
            imported.append(canonical)
            self._completed.setdefault(character, set()).add(canonical)

            instance_id = self._active.get(character, {}).pop(canonical, None)
            if instance_id is not None:
                instance = self._instances[instance_id]
                instance["status"] = "completed"
                self._latest.setdefault(character, {})[canonical] = instance_id
        return {"status": "imported", "quest_ids": imported}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def snapshot(self):
        return deepcopy({
            "version": 1,
            "quests": self._quests,
            "aliases": self._aliases,
            "factions": self._factions,
            "instances": self._instances,
            "active": self._active,
            "completed": {
                character: sorted(quests)
                for character, quests in self._completed.items()
            },
            "latest": self._latest,
            "inventory": self._inventory,
            "mailbox": self._mailbox,
            "next_instance": self._next_instance,
            "accept_seq": self._accept_seq,
        })

    def restore(self, state):
        state = deepcopy(state)

        quests = deepcopy(self._quests)
        quests.update(state.get("quests", {}))

        aliases = dict(self._aliases)
        aliases.update(state.get("aliases", {}))
        for canonical, definition in quests.items():
            aliases[canonical] = canonical
            for alias in definition.get("aliases", []):
                aliases[alias] = canonical

        completed = {}
        for character, quest_ids in state.get("completed", {}).items():
            completed[character] = {
                aliases.get(quest_id, quest_id) for quest_id in quest_ids
            }

        self._quests = quests
        self._aliases = aliases
        self._factions = deepcopy(state.get("factions", {}))
        self._instances = deepcopy(state.get("instances", {}))
        self._active = deepcopy(state.get("active", {}))
        self._completed = completed
        self._latest = deepcopy(state.get("latest", {}))
        self._inventory = deepcopy(state.get("inventory", {}))
        self._mailbox = deepcopy(state.get("mailbox", {}))
        self._next_instance = state.get("next_instance", len(self._instances))
        self._accept_seq = state.get("accept_seq", 0)

        # Repair indexes for states produced by older snapshots.
        for instance_id, instance in self._instances.items():
            character = instance["character"]
            canonical = self._resolve(instance["quest_id"])
            self._latest.setdefault(character, {})[canonical] = instance_id
            if instance["status"] == "active":
                self._active.setdefault(character, {})[canonical] = instance_id
            elif instance["status"] == "completed":
                self._completed.setdefault(character, set()).add(canonical)
        for character, active in self._active.items():
            for canonical, instance_id in list(active.items()):
                instance = self._instances.get(instance_id)
                if instance is None or instance["status"] != "active":
                    del active[canonical]
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve(self, quest_id):
        if quest_id in self._aliases:
            return self._aliases[quest_id]
        return quest_id

    def _ensure_character(self, character):
        self._active.setdefault(character, {})
        self._completed.setdefault(character, set())
        self._latest.setdefault(character, {})
        self._inventory.setdefault(character, {})
        self._mailbox.setdefault(character, [])

    def _active_instance(self, character, canonical):
        instance_id = self._active.get(character, {}).get(canonical)
        if instance_id is None:
            return None
        instance = self._instances.get(instance_id)
        if instance is None or instance["status"] != "active":
            self._active.get(character, {}).pop(canonical, None)
            return None
        return instance

    def _is_completed(self, character, canonical):
        completed = self._completed.get(character, set())
        if canonical in completed:
            return True
        return any(self._resolve(quest_id) == canonical for quest_id in completed)

    def _missing_prerequisites(self, character, canonical):
        missing = set()
        visiting = set()
        visited = set()

        def visit(quest_id):
            quest_id = self._resolve(quest_id)
            if quest_id in visited:
                return
            if quest_id in visiting:
                raise QuestError("prereq_cycle", {"quest_id": quest_id})
            definition = self._quests.get(quest_id)
            if definition is None:
                if not self._is_completed(character, quest_id):
                    missing.add(quest_id)
                return
            visiting.add(quest_id)
            for prereq in definition["prereqs"]:
                visit(prereq)
            visiting.remove(quest_id)
            visited.add(quest_id)
            if not self._is_completed(character, quest_id):
                missing.add(quest_id)

        definition = self._quests.get(canonical)
        if definition is not None:
            for prereq in definition["prereqs"]:
                visit(prereq)
        return sorted(missing)

    def _bound_item_ids(self):
        bound = set()
        for definition in self._quests.values():
            bound.update(definition["items_required"])
            bound.update(definition["grant_on_accept"])
        return bound

    def _end_active_quest(self, character, quest_id, status, now):
        canonical = self._resolve(quest_id)
        self._ensure_character(character)
        instance = self._active_instance(character, canonical)
        if instance is None:
            raise QuestError("not_active", {"quest_id": canonical})

        for item_id, count in list(instance["item_ledger"].items()):
            removable = min(count, self._inventory.get(character, {}).get(item_id, 0))
            if removable:
                self._remove_item(character, item_id, removable)
            instance["item_ledger"].pop(item_id, None)

        instance["status"] = status
        instance["ended_at"] = now
        instance["updated_at"] = now
        self._active.get(character, {}).pop(canonical, None)
        return {
            "status": status,
            "quest_id": canonical,
            "instance_id": instance["instance_id"],
        }

    def _add_item(self, character, item_id, count):
        inventory = self._inventory.setdefault(character, {})
        inventory[item_id] = inventory.get(item_id, 0) + count

    def _remove_item(self, character, item_id, count):
        inventory = self._inventory.setdefault(character, {})
        remaining = inventory.get(item_id, 0) - count
        if remaining > 0:
            inventory[item_id] = remaining
        else:
            inventory.pop(item_id, None)

    @staticmethod
    def _normalise_faction(faction):
        if faction is None:
            return None
        return str(faction).lower()

    @staticmethod
    def _count_map(values):
        result = {}
        for key, value in dict(values or {}).items():
            result[key] = int(value)
        return result

    @staticmethod
    def _positive_count(count):
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise QuestError("invalid_amount", {"amount": count})
        if count <= 0:
            raise QuestError("invalid_amount", {"amount": count})
        return count
