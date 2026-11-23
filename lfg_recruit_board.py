import copy
import threading


class LfgError(ValueError):
    """Raised when a recruitment listing or application is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class LfgRecruitBoard:
    """Recruitment listings, applications and atomic roster slots.

    Every public operation runs under one re-entrant lock so that the
    validation, roster-slot occupation and status transitions of
    ``accept`` are one atomic critical section.  All timestamps are
    opaque absolute values; timezone conversion is a client concern,
    which keeps cross-region expiry comparisons unambiguous.
    """

    def __init__(self, capacity=5, application_ttl=300):
        self.capacity = capacity
        self.application_ttl = application_ttl
        self._lock = threading.RLock()
        self._listings = {}
        self._applications = {}
        self._leader_open_listing = {}
        self._account_team = {}
        self._seq = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_listing(self, listing_id):
        listing = self._listings.get(listing_id)
        if listing is None:
            raise LfgError("unknown_listing")
        return listing

    def _get_application(self, application_id):
        application = self._applications.get(application_id)
        if application is None:
            raise LfgError("unknown_application")
        return application

    def _require_leader(self, listing, actor):
        if listing["leader"] != actor:
            raise LfgError("not_leader")

    def _applications_for(self, listing_id):
        return [app for app in self._applications.values()
                if app["listing_id"] == listing_id]

    def _member_accounts(self, listing):
        return [member["account"] for member in listing["members"]]

    def _listing_view(self, listing):
        view = {key: value for key, value in listing.items()
                if not key.startswith("_")}
        view["requirements"] = copy.deepcopy(view["requirements"])
        view["members"] = copy.deepcopy(view["members"])
        return view

    def _application_view(self, application):
        return copy.deepcopy(application)

    def _close_when_full(self, listing):
        if len(listing["members"]) >= listing["capacity"]:
            listing["status"] = "closed"
            self._leader_open_listing.pop(listing["leader"], None)

    # ------------------------------------------------------------------
    # Listing lifecycle
    # ------------------------------------------------------------------

    def create(self, listing_id, leader, capacity=None, requirements=None,
               now=0, region="r1", title="", pinned=False, dungeon=None):
        with self._lock:
            existing_id = self._leader_open_listing.get(leader)
            if existing_id is not None:
                return {"status": "existing", "listing_id": existing_id}
            if listing_id in self._listings:
                raise LfgError("listing_exists")
            self._seq += 1
            listing = {
                "listing_id": listing_id,
                "leader": leader,
                "capacity": self.capacity if capacity is None else capacity,
                "requirements": copy.deepcopy(requirements) or {},
                "region": region,
                "title": title,
                "pinned": pinned,
                "dungeon": dungeon,
                "status": "open",
                "version": 1,
                "seq": self._seq,
                "members": [{
                    "account": leader,
                    "character": None,
                    "role": "leader",
                }],
                "_member_accounts": {leader},
            }
            self._listings[listing_id] = listing
            self._leader_open_listing[leader] = listing_id
            return {
                "status": "created",
                "listing_id": listing_id,
                "version": listing["version"],
            }

    def update(self, listing_id, actor, requirements=None, title=None,
               pinned=None, now=0):
        with self._lock:
            listing = self._get_listing(listing_id)
            self._require_leader(listing, actor)
            if title is not None:
                listing["title"] = title
            if pinned is not None:
                listing["pinned"] = pinned
            if requirements is not None:
                new_requirements = copy.deepcopy(requirements)
                if new_requirements != listing["requirements"]:
                    listing["requirements"] = new_requirements
                    listing["version"] += 1
            return self._listing_view(listing)

    def apply(self, application_id, listing_id, account, character=None,
              role=None, now=0, lockouts=(), region="r1"):
        with self._lock:
            listing = self._get_listing(listing_id)
            if listing["dungeon"] is not None and listing["dungeon"] in tuple(lockouts):
                raise LfgError("lockout")
            if len(listing["members"]) >= listing["capacity"]:
                raise LfgError("roster_full")
            if listing["status"] != "open":
                raise LfgError("listing_closed")
            if account in listing["_member_accounts"]:
                raise LfgError("already_in_team")
            if account in self._account_team:
                raise LfgError("already_in_team")
            for application in self._applications_for(listing_id):
                if (application["account"] == account
                        and application["status"] in ("pending", "accepted")):
                    raise LfgError("already_applied")
            if application_id in self._applications:
                raise LfgError("application_exists")
            application = {
                "application_id": application_id,
                "listing_id": listing_id,
                "account": account,
                "character": character,
                "role": role,
                "region": region,
                "lockouts": tuple(lockouts),
                "version": listing["version"],
                "status": "pending",
                "created_at": now,
                "expires_at": now + self.application_ttl,
            }
            self._applications[application_id] = application
            return self._application_view(application)

    def accept(self, application_id, actor, now=0):
        with self._lock:
            application = self._get_application(application_id)
            listing = self._get_listing(application["listing_id"])
            self._require_leader(listing, actor)

            if application["status"] == "accepted":
                return {
                    "status": "already_accepted",
                    "application_id": application_id,
                    "listing_id": listing["listing_id"],
                    "members": self._member_accounts(listing),
                }
            if listing["status"] == "cancelled":
                raise LfgError("listing_closed")
            if application["status"] in ("expired", "rejected", "cancelled"):
                raise LfgError("application_expired")
            if now >= application["expires_at"]:
                application["status"] = "expired"
                raise LfgError("application_expired")
            if application["version"] != listing["version"]:
                raise LfgError("stale_application")

            # Atomic slot occupation: capacity check and roster write are
            # inside the same critical section, so concurrent accepts are
            # serialized and every loser sees roster_full.
            if len(listing["members"]) >= listing["capacity"]:
                raise LfgError("roster_full")
            if listing["status"] != "open":
                raise LfgError("listing_closed")
            listing["members"].append({
                "account": application["account"],
                "character": application["character"],
                "role": application["role"],
            })
            listing["_member_accounts"].add(application["account"])
            self._account_team[application["account"]] = listing["listing_id"]
            application["status"] = "accepted"
            application["accepted_at"] = now
            self._close_when_full(listing)
            return {
                "status": "accepted",
                "application_id": application_id,
                "listing_id": listing["listing_id"],
                "members": self._member_accounts(listing),
            }

    def reject(self, application_id, actor, now=0):
        with self._lock:
            application = self._get_application(application_id)
            listing = self._get_listing(application["listing_id"])
            self._require_leader(listing, actor)
            if application["status"] == "pending":
                application["status"] = "rejected"
                application["rejected_at"] = now
            return self._application_view(application)

    def expire(self, application_id, now=0):
        with self._lock:
            application = self._get_application(application_id)
            if application["status"] == "pending":
                application["status"] = "expired"
                application["expired_at"] = now
            return self._application_view(application)

    def cancel(self, listing_id, actor, now=0):
        with self._lock:
            listing = self._get_listing(listing_id)
            self._require_leader(listing, actor)
            for application in self._applications_for(listing_id):
                if application["status"] == "pending":
                    application["status"] = "cancelled"
                    application["cancelled_at"] = now
            listing["status"] = "cancelled"
            self._leader_open_listing.pop(listing["leader"], None)
            return {"status": "cancelled", "listing_id": listing_id}

    def get_listing(self, listing_id):
        with self._lock:
            return self._listing_view(self._get_listing(listing_id))

    def applications(self, listing_id=None):
        with self._lock:
            if listing_id is None:
                applications = list(self._applications.values())
            else:
                applications = self._applications_for(listing_id)
            return [self._application_view(application)
                    for application in applications]

    def members(self, listing_id):
        with self._lock:
            listing = self._get_listing(listing_id)
            return copy.deepcopy(listing["members"])

    def listings(self, region=None, now=0):
        with self._lock:
            open_listings = [listing for listing in self._listings.values()
                             if listing["status"] == "open"]
            if region is not None:
                open_listings = [listing for listing in open_listings
                                 if listing["region"] == region]
            open_listings.sort(key=lambda listing: (not listing["pinned"],
                                                    listing["seq"]))
            return [self._listing_view(listing) for listing in open_listings]

    def snapshot(self):
        with self._lock:
            return {
                "capacity": self.capacity,
                "application_ttl": self.application_ttl,
                "seq": self._seq,
                "listings": [self._listing_view(listing)
                             for listing in self._listings.values()],
                "applications": [self._application_view(application)
                                 for application in self._applications.values()],
                "leader_open_listing": dict(self._leader_open_listing),
                "account_team": dict(self._account_team),
            }

    def restore(self, state):
        with self._lock:
            self.capacity = state.get("capacity", self.capacity)
            self.application_ttl = state.get("application_ttl",
                                             self.application_ttl)
            self._seq = state.get("seq", 0)
            self._listings = {}
            for saved in state.get("listings", []):
                listing = copy.deepcopy(saved)
                listing["_member_accounts"] = {
                    member["account"] for member in listing["members"]
                }
                self._listings[listing["listing_id"]] = listing
            self._applications = {
                application["application_id"]: copy.deepcopy(application)
                for application in state.get("applications", [])
            }
            self._leader_open_listing = {}
            self._account_team = {}
            for listing in self._listings.values():
                if listing["status"] == "open":
                    self._leader_open_listing[listing["leader"]] = \
                        listing["listing_id"]
                for member in listing["members"]:
                    if member["account"] != listing["leader"]:
                        self._account_team.setdefault(member["account"],
                                                      listing["listing_id"])
