class AllianceError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


_WAR_ROLES = {"leader", "officer"}


class GuildAlliancePact:
    """Alliance membership, invitations, wars and merged identities."""

    def __init__(self):
        self.capacity = 10
        self._alliances = {}
        self._guild_to_alliance = {}
        self._invites = {}
        self._invite_seq = 0
        self._alliance_seq = 0
        self._wars = []
        self._kill_events = set()
        self._messages = {}
        self._warehouse = {}
        self._archived_warehouse = {}

    def _get_alliance(self, alliance_id):
        alliance = self._alliances.get(alliance_id)
        if alliance is None:
            raise AllianceError("ALLIANCE_NOT_FOUND", alliance_id)
        return alliance

    def create_alliance(self, tag, leader_guild, capacity=10):
        if leader_guild in self._guild_to_alliance:
            raise AllianceError("GUILD_ALREADY_IN_ALLIANCE", leader_guild)
        for alliance in self._alliances.values():
            if alliance["tag"] == tag:
                raise AllianceError("TAG_TAKEN", tag)
        self._alliance_seq += 1
        alliance_id = "ally-%d" % self._alliance_seq
        self._alliances[alliance_id] = {
            "tag": tag,
            "leader": leader_guild,
            "capacity": capacity,
            "members": [leader_guild],
            "disbanded": False,
        }
        self._guild_to_alliance[leader_guild] = alliance_id
        self._messages[alliance_id] = []
        self._warehouse[alliance_id] = {}
        return {"alliance_id": alliance_id, "tag": tag}

    def invite(self, alliance_id, guild_id, now=0, expires_at=0, version=1):
        alliance = self._get_alliance(alliance_id)
        if alliance["disbanded"]:
            raise AllianceError("ALLIANCE_NOT_FOUND", alliance_id)
        self._invite_seq += 1
        invite_id = "inv-%d" % self._invite_seq
        self._invites[invite_id] = {
            "alliance_id": alliance_id,
            "guild_id": guild_id,
            "expires_at": expires_at,
            "version": version,
            "used": False,
        }
        return {"invite_id": invite_id, "alliance_id": alliance_id,
                "expires_at": expires_at, "version": version}

    def accept_invite(self, invite_id, guild_id, now=0, version=1):
        invite = self._invites.get(invite_id)
        if invite is None or invite["used"]:
            raise AllianceError("INVITE_NOT_FOUND", invite_id)
        if invite["guild_id"] != guild_id:
            raise AllianceError("INVITE_NOT_FOUND", invite_id)
        if invite["version"] != version:
            raise AllianceError("INVITE_STALE", invite_id)
        if invite["expires_at"] and now > invite["expires_at"]:
            raise AllianceError("EXPIRED_INVITE", invite_id)
        alliance_id = invite["alliance_id"]
        alliance = self._get_alliance(alliance_id)
        if alliance["disbanded"]:
            raise AllianceError("ALLIANCE_NOT_FOUND", alliance_id)
        if guild_id in self._guild_to_alliance:
            raise AllianceError("GUILD_ALREADY_IN_ALLIANCE", guild_id)
        if len(alliance["members"]) >= alliance["capacity"]:
            raise AllianceError("ALLIANCE_FULL", alliance_id)
        invite["used"] = True
        alliance["members"].append(guild_id)
        self._guild_to_alliance[guild_id] = alliance_id
        return {"alliance_id": alliance_id, "guild_id": guild_id}

    def leave(self, guild_id):
        alliance_id = self._guild_to_alliance.pop(guild_id, None)
        if alliance_id is None:
            raise AllianceError("GUILD_NOT_IN_ALLIANCE", guild_id)
        alliance = self._alliances[alliance_id]
        if guild_id in alliance["members"]:
            alliance["members"].remove(guild_id)
        return {"alliance_id": alliance_id, "guild_id": guild_id}

    def disband(self, alliance_id):
        alliance = self._get_alliance(alliance_id)
        if alliance["disbanded"]:
            raise AllianceError("ALLIANCE_NOT_FOUND", alliance_id)
        alliance["disbanded"] = True
        for member in list(alliance["members"]):
            self._guild_to_alliance.pop(member, None)
        alliance["members"] = []
        for war in self._wars:
            if war["active"] and (war["attacker"] == alliance_id
                                  or war["target"] == alliance_id):
                war["active"] = False
        archived = sum(self._warehouse.get(alliance_id, {}).values())
        self._archived_warehouse[alliance_id] = self._warehouse.get(
            alliance_id, {})
        self._warehouse[alliance_id] = {}
        for invite in self._invites.values():
            if invite["alliance_id"] == alliance_id:
                invite["used"] = True
        return {"alliance_id": alliance_id, "archived_warehouse": archived}

    def declare_war(self, alliance_id, target_alliance, actor, role="member"):
        alliance = self._get_alliance(alliance_id)
        target = self._get_alliance(target_alliance)
        if alliance["disbanded"] or target["disbanded"]:
            raise AllianceError("ALLIANCE_NOT_FOUND", alliance_id)
        if role not in _WAR_ROLES:
            raise AllianceError("PERMISSION_DENIED", actor)
        if actor not in alliance["members"]:
            raise AllianceError("PERMISSION_DENIED", actor)
        war = {"attacker": alliance_id, "target": target_alliance,
               "active": True}
        self._wars.append(war)
        return war

    def record_kill(self, event_id, killer_guild, victim_guild):
        if event_id in self._kill_events:
            return {"event_id": event_id, "scored": False, "duplicate": True}
        self._kill_events.add(event_id)
        return {"event_id": event_id, "scored": True, "duplicate": False}

    def are_friendly(self, guild_a, guild_b):
        ally_a = self._guild_to_alliance.get(guild_a)
        ally_b = self._guild_to_alliance.get(guild_b)
        if ally_a is None or ally_b is None or ally_a != ally_b:
            return False
        return not self._alliances[ally_a]["disbanded"]

    def post_message(self, alliance_id, guild_id, text):
        alliance = self._get_alliance(alliance_id)
        if alliance["disbanded"] or guild_id not in alliance["members"]:
            raise AllianceError("PERMISSION_DENIED", guild_id)
        self._messages.setdefault(alliance_id, []).append(text)
        return {"alliance_id": alliance_id, "guild_id": guild_id}

    def channel_history(self, alliance_id, guild_id):
        alliance = self._alliances.get(alliance_id)
        if alliance is None or alliance["disbanded"]:
            return []
        if guild_id not in alliance["members"]:
            return []
        return list(self._messages.get(alliance_id, []))

    def channel_members(self, alliance_id):
        alliance = self._alliances.get(alliance_id)
        if alliance is None or alliance["disbanded"]:
            return []
        return list(alliance["members"])

    def warehouse_contrib(self, alliance_id, guild_id, amount=0):
        alliance = self._get_alliance(alliance_id)
        if alliance["disbanded"] or guild_id not in alliance["members"]:
            raise AllianceError("PERMISSION_DENIED", guild_id)
        contrib = self._warehouse.setdefault(alliance_id, {})
        contrib[guild_id] = contrib.get(guild_id, 0) + amount
        return {"alliance_id": alliance_id, "guild_id": guild_id,
                "total": contrib[guild_id]}

    def wars(self, alliance_id):
        return [dict(w) for w in self._wars
                if w["attacker"] == alliance_id or w["target"] == alliance_id]

    def merge_servers(self, mapping):
        for old_id, new_id in mapping.items():
            if old_id not in self._alliances:
                continue
            alliance = self._alliances.pop(old_id)
            self._alliances[new_id] = alliance
            for member in alliance["members"]:
                self._guild_to_alliance[member] = new_id
            if old_id in self._messages:
                self._messages[new_id] = self._messages.pop(old_id)
            if old_id in self._warehouse:
                self._warehouse[new_id] = self._warehouse.pop(old_id)
            for invite in self._invites.values():
                if invite["alliance_id"] == old_id:
                    invite["alliance_id"] = new_id
        for war in self._wars:
            if war["attacker"] in mapping:
                war["attacker"] = mapping[war["attacker"]]
            if war["target"] in mapping:
                war["target"] = mapping[war["target"]]
        return dict(mapping)

    def snapshot(self):
        return self.dump()

    def dump(self):
        return {
            "alliances": {aid: dict(a, members=list(a["members"]))
                          for aid, a in self._alliances.items()},
            "guild_to_alliance": dict(self._guild_to_alliance),
            "invites": {iid: dict(inv) for iid, inv in self._invites.items()},
            "invite_seq": self._invite_seq,
            "alliance_seq": self._alliance_seq,
            "wars": [dict(w) for w in self._wars],
            "kill_events": sorted(self._kill_events),
            "messages": {aid: list(m) for aid, m in self._messages.items()},
            "warehouse": {aid: dict(w) for aid, w in self._warehouse.items()},
            "archived_warehouse": {aid: dict(w) for aid, w in
                                   self._archived_warehouse.items()},
        }

    def load(self, state):
        self._alliances = {aid: dict(a, members=list(a["members"]))
                           for aid, a in state["alliances"].items()}
        self._guild_to_alliance = dict(state["guild_to_alliance"])
        self._invites = {iid: dict(inv)
                         for iid, inv in state["invites"].items()}
        self._invite_seq = state["invite_seq"]
        self._alliance_seq = state["alliance_seq"]
        self._wars = [dict(w) for w in state["wars"]]
        self._kill_events = set(state["kill_events"])
        self._messages = {aid: list(m)
                          for aid, m in state["messages"].items()}
        self._warehouse = {aid: dict(w)
                           for aid, w in state["warehouse"].items()}
        self._archived_warehouse = {aid: dict(w) for aid, w in
                                    state["archived_warehouse"].items()}
