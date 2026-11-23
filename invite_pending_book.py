import threading


class InviteError(ValueError):
    """Raised when a party invite operation is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class InvitePendingBook:
    """Party invites with expiry, account routing and atomic acceptance."""

    def __init__(self, ttl=60, decline_cooldown=30):
        self.ttl = ttl
        self.decline_cooldown = decline_cooldown
        self._lock = threading.RLock()
        self._teams = {}
        self._invites = {}
        self._by_account = {}
        self._account_team = {}
        self._blocks = set()
        self._cooldowns = {}

    def create_team(self, team_id, leader, capacity=5, now=0):
        with self._lock:
            self._teams[team_id] = {
                "team_id": team_id,
                "leader": leader,
                "capacity": capacity,
                "members": [leader],
                "created_at": now,
            }
            self._account_team[leader] = team_id

    def disband(self, team_id, actor, now=0):
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                raise InviteError("team_disbanded")
            for member in team["members"]:
                if self._account_team.get(member) == team_id:
                    del self._account_team[member]
            del self._teams[team_id]
            for invite in list(self._invites.values()):
                if invite["team_id"] == team_id and invite["status"] == "pending":
                    invite["status"] = "invalidated"
                    self._detach_index(invite)
            return {"status": "disbanded", "team_id": team_id}

    def block(self, account, target):
        with self._lock:
            self._blocks.add((account, target))

    def _active_invite(self, team_id, to_account):
        for invite_id in self._by_account.get(to_account, ()):
            invite = self._invites[invite_id]
            if invite["team_id"] == team_id and invite["status"] == "pending":
                return invite
        return None

    def send(self, invite_id, team_id, from_account, to_account, now=0):
        with self._lock:
            if (to_account, from_account) in self._blocks:
                raise InviteError("blocked")
            until = self._cooldowns.get((from_account, to_account))
            if until is not None and now < until:
                raise InviteError("cooldown")
            team = self._teams.get(team_id)
            if team is None:
                raise InviteError("team_disbanded")
            existing = self._active_invite(team_id, to_account)
            if existing is not None:
                return {"status": "already_invited", "invite_id": existing["invite_id"]}
            invite = {
                "invite_id": invite_id,
                "team_id": team_id,
                "from_account": from_account,
                "to_account": to_account,
                "sent_at": now,
                "expires_at": now + self.ttl,
                "status": "pending",
            }
            self._invites[invite_id] = invite
            self._by_account.setdefault(to_account, []).append(invite_id)
            return {"status": "sent", "invite_id": invite_id,
                    "expires_at": invite["expires_at"]}

    def accept(self, invite_id, account, now=0, in_dungeon=False):
        with self._lock:
            invite = self._invites.get(invite_id)
            if invite is None:
                raise InviteError("unknown_invite")
            if invite["status"] == "accepted":
                raise InviteError("already_joined")
            if invite["to_account"] != account:
                raise InviteError("unknown_invite")
            if now >= invite["expires_at"] or invite["status"] == "expired":
                if invite["status"] == "pending":
                    invite["status"] = "expired"
                    self._detach_index(invite)
                raise InviteError("invite_expired")
            if account in self._account_team:
                current = self._account_team[account]
                team = self._teams.get(current)
                if team is None or team["leader"] != account or len(team["members"]) > 1:
                    raise InviteError("already_joined")
            if (account, invite["from_account"]) in self._blocks:
                raise InviteError("blocked")
            if in_dungeon:
                raise InviteError("in_dungeon")
            team = self._teams.get(invite["team_id"])
            if team is None:
                invite["status"] = "invalidated"
                self._detach_index(invite)
                raise InviteError("team_disbanded")
            if len(team["members"]) >= team["capacity"]:
                raise InviteError("team_full")

            current = self._account_team.get(account)
            if current is not None:
                solo_team = self._teams.pop(current)
                del self._account_team[account]
                for other in list(self._invites.values()):
                    if other["team_id"] == current and other["status"] == "pending":
                        other["status"] = "invalidated"
                        self._detach_index(other)

            team["members"].append(account)
            self._account_team[account] = team["team_id"]
            invite["status"] = "accepted"
            self._detach_index(invite)
            return {"status": "accepted", "team_id": team["team_id"],
                    "members": list(team["members"])}

    def decline(self, invite_id, account, now=0):
        with self._lock:
            invite = self._invites.get(invite_id)
            if invite is None or invite["to_account"] != account:
                raise InviteError("unknown_invite")
            if invite["status"] != "pending":
                raise InviteError("unknown_invite")
            invite["status"] = "declined"
            self._detach_index(invite)
            self._cooldowns[(invite["from_account"], account)] = (
                now + self.decline_cooldown)
            return {"status": "declined", "invite_id": invite_id}

    def expire(self, invite_id, now=0):
        with self._lock:
            invite = self._invites.get(invite_id)
            if invite is None:
                raise InviteError("unknown_invite")
            if invite["status"] != "pending":
                raise InviteError("invite_expired")
            invite["status"] = "expired"
            self._detach_index(invite)
            return {"status": "expired", "invite_id": invite_id}

    def pending(self, account, now=0):
        with self._lock:
            rows = []
            for invite_id in self._by_account.get(account, ()):
                invite = self._invites[invite_id]
                if invite["status"] != "pending":
                    continue
                if now >= invite["expires_at"]:
                    invite["status"] = "expired"
                    self._detach_index(invite)
                    continue
                rows.append({
                    "invite_id": invite["invite_id"],
                    "team_id": invite["team_id"],
                    "from_account": invite["from_account"],
                    "to_account": invite["to_account"],
                    "sent_at": invite["sent_at"],
                    "expires_at": invite["expires_at"],
                })
            return rows

    def team(self, team_id):
        with self._lock:
            team = self._teams.get(team_id)
            return None if team is None else self._team_view(team)

    def members(self, team_id):
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                return []
            return list(team["members"])

    def dump(self):
        with self._lock:
            return {
                "ttl": self.ttl,
                "decline_cooldown": self.decline_cooldown,
                "teams": [self._team_view(team) for team in self._teams.values()],
                "invites": [dict(invite) for invite in self._invites.values()
                            if invite["status"] == "pending"],
                "blocks": sorted(self._blocks),
                "cooldowns": [list(item) for item in self._cooldowns.items()],
            }

    def load(self, state):
        with self._lock:
            self.ttl = state.get("ttl", self.ttl)
            self.decline_cooldown = state.get("decline_cooldown",
                                               self.decline_cooldown)
            self._teams = {}
            self._invites = {}
            self._by_account = {}
            self._account_team = {}
            for team in state.get("teams", []):
                self._teams[team["team_id"]] = {
                    "team_id": team["team_id"],
                    "leader": team["leader"],
                    "capacity": team["capacity"],
                    "members": list(team["members"]),
                    "created_at": team.get("created_at", 0),
                }
                for member in team["members"]:
                    self._account_team[member] = team["team_id"]
            for invite in state.get("invites", []):
                if invite.get("status") != "pending":
                    continue
                invite = dict(invite)
                invite["status"] = "pending"
                self._invites[invite["invite_id"]] = invite
                self._by_account.setdefault(invite["to_account"], []).append(
                    invite["invite_id"])
            self._blocks = {tuple(item) for item in state.get("blocks", [])}
            self._cooldowns = {
                tuple(key): until for key, until in state.get("cooldowns", [])}

    def _detach_index(self, invite):
        index = self._by_account.get(invite["to_account"])
        if index and invite["invite_id"] in index:
            index.remove(invite["invite_id"])

    @staticmethod
    def _team_view(team):
        return {
            "team_id": team["team_id"],
            "leader": team["leader"],
            "capacity": team["capacity"],
            "members": list(team["members"]),
            "created_at": team["created_at"],
        }
