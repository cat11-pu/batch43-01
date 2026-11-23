import copy


class TribunalError(ValueError):
    """Raised when a tribunal operation is rejected; ``code`` is machine readable."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReportAbuseTribunal:
    """Abuse report intake, merging, review, penalties, appeals and evidence."""

    _ACTIVE_BAN_STATES = ("active", "upheld")
    _REVIEWED_TICKET_STATES = ("upheld", "dismissed", "reviewed")

    def __init__(self, merge_window=300, cooldown=60, ban_duration=3600):
        self.merge_window = merge_window
        self.cooldown = cooldown
        self.ban_duration = ban_duration
        self._tickets = {}
        self._ticket_order = []
        self._reports = {}
        self._accounts = {}
        self._bans = {}
        self._ban_order = []
        self._replays = {}
        self._audit = []
        self._last_report_at = {}
        self._ticket_seq = 0
        self._ban_seq = 0

    def _require_account(self, account_id):
        account = self._accounts.get(account_id)
        if account is None:
            raise TribunalError("account_not_found", {"account_id": account_id})
        return account

    def _ensure_account(self, account_id):
        account = self._accounts.get(account_id)
        if account is None:
            account = {
                "account_id": account_id,
                "name": account_id,
                "aliases": [],
                "server": None,
                "compensation": 0,
            }
            self._accounts[account_id] = account
        return account

    def _require_ticket(self, ticket_id):
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise TribunalError("ticket_not_found", {"ticket_id": ticket_id})
        return ticket

    def _require_ban(self, ban_id):
        ban = self._bans.get(ban_id)
        if ban is None:
            raise TribunalError("ban_not_found", {"ban_id": ban_id})
        return ban

    def _record(self, action, actor, at, **details):
        event = {"action": action, "actor": actor, "at": at}
        event.update(details)
        self._audit.append(event)

    def _ticket_view(self, ticket):
        view = copy.deepcopy(ticket)
        account = self._accounts.get(ticket["target_id"])
        if account is not None:
            view["target_name"] = account["name"]
        view["report_count"] = len(ticket["reports"])
        return view

    def _ban_view(self, ban):
        return copy.deepcopy(ban)

    def _ban_is_active(self, ban):
        return ban["status"] in self._ACTIVE_BAN_STATES

    def _normalise_evidence(self, evidence, category, audio, report_id,
                            reporter_id, now):
        if evidence is None:
            raw_items = []
        elif isinstance(evidence, (str, bytes, dict)):
            raw_items = [evidence]
        else:
            try:
                raw_items = list(evidence)
            except TypeError:
                raw_items = [evidence]

        items = []
        for raw in raw_items:
            if isinstance(raw, dict):
                item = copy.deepcopy(raw)
                if "audio" in item and "kind" not in item:
                    item["kind"] = "audio"
                elif "text" in item and "kind" not in item:
                    item["kind"] = "text"
            elif isinstance(raw, bytes):
                item = {"kind": "audio", "audio": copy.deepcopy(raw)}
            else:
                item = {"kind": "text", "text": copy.deepcopy(raw)}
            item.setdefault("report_id", report_id)
            item.setdefault("reporter_id", reporter_id)
            item.setdefault("at", now)
            item.setdefault("category", category)
            items.append(item)

        if audio is not None:
            items.append({
                "kind": "audio",
                "audio": copy.deepcopy(audio),
                "report_id": report_id,
                "reporter_id": reporter_id,
                "at": now,
                "category": category,
            })
        return items

    def file_report(self, report_id, reporter_id, target_id, reason, now=0,
                    evidence=None, match_id=None, server=None,
                    target_server=None, device_id=None, category="text",
                    audio=None):
        if not report_id or not reporter_id or not target_id:
            raise TribunalError("invalid_report")
        if report_id in self._reports:
            raise TribunalError("duplicate_report", {"report_id": report_id})

        last_report = self._last_report_at.get(reporter_id)
        if last_report is not None and now - last_report < self.cooldown:
            raise TribunalError("cooldown", {
                "reporter_id": reporter_id,
                "retry_after": self.cooldown - (now - last_report),
            })

        reporter = self._ensure_account(reporter_id)
        target = self._ensure_account(target_id)
        if server is not None:
            reporter["server"] = server
        if target_server is not None:
            target["server"] = target_server

        ticket = None
        for candidate in self._tickets.values():
            if candidate["target_id"] != target_id:
                continue
            if candidate["status"] != "pending":
                continue
            if now <= candidate["created_at"] + self.merge_window:
                ticket = candidate
                break

        if ticket is None:
            self._ticket_seq += 1
            ticket_id = "t%d" % self._ticket_seq
            routed_server = target_server or target.get("server") or server
            ticket = {
                "ticket_id": ticket_id,
                "target_id": target_id,
                "target_name": target["name"],
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "window_end": now + self.merge_window,
                "server": routed_server,
                "target_server": target_server,
                "reason": reason,
                "reports": [],
                "evidence": [],
                "match_ids": [],
                "categories": [],
                "reviewer": None,
                "reviewed_at": None,
                "review_reason": None,
                "ban_id": None,
            }
            self._tickets[ticket_id] = ticket
            self._ticket_order.append(ticket_id)
        elif target_server is not None:
            ticket["server"] = target_server
            ticket["target_server"] = target_server

        ticket["updated_at"] = now
        if match_id is not None and match_id not in ticket["match_ids"]:
            ticket["match_ids"].append(match_id)
        if category not in ticket["categories"]:
            ticket["categories"].append(category)

        report = {
            "report_id": report_id,
            "reporter_id": reporter_id,
            "target_id": target_id,
            "reason": reason,
            "at": now,
            "server": server,
            "target_server": target_server,
            "device_id": device_id,
            "category": category,
            "match_id": match_id,
            "withdrawn": False,
            "withdrawn_at": None,
        }
        ticket["reports"].append(report)
        self._reports[report_id] = report
        ticket["evidence"].extend(
            self._normalise_evidence(
                evidence, category, audio, report_id, reporter_id, now
            )
        )
        self._last_report_at[reporter_id] = now
        self._record(
            "report",
            reporter_id,
            now,
            report_id=report_id,
            ticket_id=ticket["ticket_id"],
            target_id=target_id,
        )
        return self._ticket_view(ticket)

    def withdraw(self, ticket_id, reporter_id, now=0):
        ticket = self._require_ticket(ticket_id)
        if ticket["status"] == "withdrawn":
            raise TribunalError("ticket_withdrawn", {"ticket_id": ticket_id})
        if ticket["status"] in self._REVIEWED_TICKET_STATES:
            raise TribunalError("already_reviewed", {"ticket_id": ticket_id})

        own_reports = [
            report for report in ticket["reports"]
            if report["reporter_id"] == reporter_id
        ]
        if not own_reports:
            raise TribunalError("not_reporter", {
                "ticket_id": ticket_id,
                "reporter_id": reporter_id,
            })

        for report in own_reports:
            report["withdrawn"] = True
            report["withdrawn_at"] = now

        if all(report["withdrawn"] for report in ticket["reports"]):
            ticket["status"] = "withdrawn"
        ticket["updated_at"] = now
        self._record(
            "withdraw",
            reporter_id,
            now,
            ticket_id=ticket_id,
        )
        return self._ticket_view(ticket)

    def review(self, ticket_id, reviewer, decision, reason=None, now=0):
        ticket = self._require_ticket(ticket_id)
        if ticket["status"] == "withdrawn":
            raise TribunalError("ticket_withdrawn", {"ticket_id": ticket_id})
        if ticket["status"] in self._REVIEWED_TICKET_STATES:
            raise TribunalError("already_reviewed", {"ticket_id": ticket_id})
        if decision not in ("upheld", "dismissed"):
            raise TribunalError("invalid_decision", {"decision": decision})
        if not isinstance(reason, str) or not reason.strip():
            raise TribunalError("reason_required", {"ticket_id": ticket_id})

        ticket["status"] = decision
        ticket["reviewer"] = reviewer
        ticket["reviewed_at"] = now
        ticket["review_reason"] = reason

        ban = None
        if decision == "upheld":
            ban = self._create_ban(ticket, reviewer, reason, now)
            ticket["ban_id"] = ban["ban_id"]

        ticket["updated_at"] = now
        self._record(
            "review",
            reviewer,
            now,
            ticket_id=ticket_id,
            decision=decision,
            reason=reason,
            ban_id=ban["ban_id"] if ban is not None else None,
        )
        return self._ticket_view(ticket)

    def _create_ban(self, ticket, reviewer, reason, now):
        self._ban_seq += 1
        ban_id = "b%d" % self._ban_seq
        ban = {
            "ban_id": ban_id,
            "account_id": ticket["target_id"],
            "target_id": ticket["target_id"],
            "ticket_id": ticket["ticket_id"],
            "server": ticket.get("server"),
            "status": "active",
            "reason": reason,
            "actor": reviewer,
            "started_at": now,
            "expires_at": now + self.ban_duration,
            "duration": self.ban_duration,
            "created_at": now,
            "appealed": False,
            "appeal_status": None,
            "revoked_at": None,
            "revoked_by": None,
            "expired_at": None,
            "compensation": 0,
        }
        self._bans[ban_id] = ban
        self._ban_order.append(ban_id)
        return ban

    def appeal(self, ban_id, reviewer, granted, now=0, reason=None):
        ban = self._require_ban(ban_id)
        if ban.get("appealed"):
            raise TribunalError("already_appealed", {"ban_id": ban_id})
        if not self._ban_is_active(ban):
            raise TribunalError("ban_inactive", {
                "ban_id": ban_id,
                "status": ban["status"],
            })

        account = self._ensure_account(ban["account_id"])
        ban["appealed"] = True
        ban["appeal_status"] = "granted" if granted else "denied"
        ban["appealed_at"] = now
        ban["appealed_by"] = reviewer
        ban["appeal_reason"] = reason

        if granted:
            served = max(
                0,
                min(now, ban["expires_at"]) - ban["started_at"],
            )
            ban["status"] = "revoked"
            ban["revoked_at"] = now
            ban["revoked_by"] = reviewer
            ban["compensation"] = served
            account["compensation"] += served
        else:
            ban["status"] = "upheld"

        self._record(
            "appeal",
            reviewer,
            now,
            ban_id=ban_id,
            account_id=ban["account_id"],
            granted=bool(granted),
            reason=reason,
        )
        return self._ban_view(ban)

    def tick(self, now=0):
        expired = []
        for ban_id in self._ban_order:
            ban = self._bans[ban_id]
            if not self._ban_is_active(ban):
                continue
            if now < ban["expires_at"]:
                continue
            ban["status"] = "expired"
            ban["expired_at"] = now
            ban["expired_by"] = "system"
            expired.append(ban_id)
            self._record(
                "expire",
                "system",
                now,
                ban_id=ban_id,
                account_id=ban["account_id"],
            )
        return {"expired": expired}

    def rename(self, account_id, new_name):
        account = self._require_account(account_id)
        old_name = account["name"]
        if old_name != new_name and old_name not in account["aliases"]:
            account["aliases"].append(old_name)
        account["name"] = new_name
        for ticket in self._tickets.values():
            if ticket["target_id"] == account_id:
                ticket["target_name"] = new_name
        return copy.deepcopy(account)

    def store_replay(self, match_id, payload):
        if match_id in self._replays:
            raise TribunalError("replay_exists", {"match_id": match_id})
        stored = copy.deepcopy(payload)
        self._replays[match_id] = stored
        return copy.deepcopy(stored)

    def replay(self, match_id):
        if match_id not in self._replays:
            raise TribunalError("replay_not_found", {"match_id": match_id})
        return copy.deepcopy(self._replays[match_id])

    def ticket(self, ticket_id):
        return self._ticket_view(self._require_ticket(ticket_id))

    def tickets(self, target_id=None):
        values = [self._tickets[ticket_id] for ticket_id in self._ticket_order]
        if target_id is not None:
            values = [
                ticket for ticket in values
                if ticket["target_id"] == target_id
            ]
        return [self._ticket_view(ticket) for ticket in values]

    def evidence(self, ticket_id):
        ticket = self._require_ticket(ticket_id)
        return copy.deepcopy(ticket["evidence"])

    def bans(self, account_id=None):
        values = [self._bans[ban_id] for ban_id in self._ban_order]
        if account_id is not None:
            values = [
                ban for ban in values
                if ban["account_id"] == account_id
            ]
        return [self._ban_view(ban) for ban in values]

    def account(self, account_id):
        account = self._require_account(account_id)
        active_bans = [
            ban for ban in self._bans.values()
            if ban["account_id"] == account_id and self._ban_is_active(ban)
        ]
        active_bans.sort(key=lambda item: item["started_at"])
        view = copy.deepcopy(account)
        view["banned"] = bool(active_bans)
        view["active_ban_id"] = (
            active_bans[0]["ban_id"] if active_bans else None
        )
        view["banned_until"] = (
            max(ban["expires_at"] for ban in active_bans)
            if active_bans else None
        )
        return view

    def audit(self):
        return copy.deepcopy(self._audit)
