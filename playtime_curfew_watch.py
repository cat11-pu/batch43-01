class PlaytimeError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class PlaytimeCurfewWatch:
    """Account-level playtime accumulation, curfew and compensation.

    Time is always server time: epoch seconds passed in by the caller. The
    client timezone offset is accepted only for logging/observability and
    never participates in any decision.
    """

    def __init__(self, daily_limit_minutes=90, holiday_limit_minutes=180,
                 curfew=(22, 8), compensation_cap_minutes=120):
        self.daily_limit_minutes = daily_limit_minutes
        self.holiday_limit_minutes = holiday_limit_minutes
        self.curfew = curfew
        self.compensation_cap_minutes = compensation_cap_minutes
        self._intervals = {}
        self._birth_years = {}
        self._holidays = set()
        self._compensation = {}
        self._batches = {}
        self._unbound = set()
        self._reminders = set()
        self._recovery = {}

    @staticmethod
    def _day_of(ts):
        return int(ts // 86400)

    @staticmethod
    def _merge_intervals(intervals, start, end):
        merged = []
        inserted = False
        for existing_start, existing_end in sorted(intervals):
            if existing_end < start:
                merged.append((existing_start, existing_end))
            elif existing_start > end:
                if not inserted:
                    merged.append((start, end))
                    inserted = True
                merged.append((existing_start, existing_end))
            else:
                start = min(start, existing_start)
                end = max(end, existing_end)
        if not inserted:
            merged.append((start, end))
        return merged

    def _add_interval(self, account_id, start, end):
        if end <= start:
            return
        intervals = self._intervals.setdefault(account_id, [])
        intervals[:] = self._merge_intervals(intervals, start, end)

    def _is_adult(self, account_id, current_year):
        birth_year = self._birth_years.get(account_id)
        return birth_year is not None and current_year - birth_year >= 18

    def _limit_minutes(self, account_id, day):
        base = (self.holiday_limit_minutes if day in self._holidays
                else self.daily_limit_minutes)
        return base + self._compensation.get((account_id, day), 0)

    def _in_curfew(self, ts):
        start_hour, end_hour = self.curfew
        hour = int((ts % 86400) // 3600)
        if start_hour > end_hour:
            return hour >= start_hour or hour < end_hour
        if start_hour < end_hour:
            return start_hour <= hour < end_hour
        return False

    def record_session(self, account_id, device_id, seconds, now,
                       afk_seconds=0):
        counted = max(0, int(seconds) - max(0, int(afk_seconds)))
        end = int(now)
        start = end - counted
        self._add_interval(account_id, start, end)
        return {"counted_seconds": counted, "device_id": device_id}

    def total_minutes(self, account_id, day):
        day_start = int(day) * 86400
        day_end = day_start + 86400
        total = 0
        for start, end in self._intervals.get(account_id, ()):
            total += max(0, min(end, day_end) - max(start, day_start))
        return total / 60.0

    def check(self, account_id, now, client_tz_offset=0, foreign_minutes=0,
              current_year=2026):
        now = int(now)
        if self._is_adult(account_id, current_year) or \
                account_id in self._unbound:
            return {"allowed": True, "reason": None}

        if foreign_minutes is None:
            return {"allowed": False, "reason": "CROSS_GAME_UNAVAILABLE"}

        if self._in_curfew(now):
            return {"allowed": False, "reason": "CURFEW"}

        day = self._day_of(now)
        limit = self._limit_minutes(account_id, day)
        used = self.total_minutes(account_id, day) + float(foreign_minutes)
        if used >= limit:
            return {"allowed": False, "reason": "DAILY_LIMIT"}
        return {"allowed": True, "reason": None}

    def set_birthdate(self, account_id, year):
        self._birth_years[account_id] = int(year)

    def set_holiday(self, day, is_holiday=True):
        day = int(day)
        if is_holiday:
            self._holidays.add(day)
        else:
            self._holidays.discard(day)

    def compensate(self, account_id, minutes, batch_id, day):
        key = (account_id, int(day))
        if batch_id in self._batches:
            return {"granted": 0, "duplicate": True}
        self._batches[batch_id] = key
        already = self._compensation.get(key, 0)
        granted = max(0, min(int(minutes),
                             self.compensation_cap_minutes - already))
        self._compensation[key] = already + granted
        return {"granted": granted, "duplicate": False}

    def set_parental_unbind(self, account_id, value=True):
        if value:
            self._unbound.add(account_id)
        else:
            self._unbound.discard(account_id)

    def link_guest(self, guest_id, account_id, now):
        day = self._day_of(now)
        merged = self.total_minutes(guest_id, day)
        for start, end in self._intervals.pop(guest_id, ()):
            self._add_interval(account_id, start, end)
        return {"merged_minutes": merged}

    def force_offline(self, account_id, now):
        day = self._day_of(now)
        minutes = self.total_minutes(account_id, day)
        token = "recovery:%s:%s:%s" % (account_id, day, int(now))
        self._recovery[(account_id, day)] = {
            "recovery_token": token,
            "progress_saved": True,
            "saved_at": int(now),
        }
        return {
            "progress_saved": True,
            "recovery_token": token,
            "minutes": minutes,
        }

    def reminder_due(self, account_id, now):
        now = int(now)
        day = self._day_of(now)
        key = (account_id, day)
        if key in self._reminders:
            return False
        if self._is_adult(account_id, 2026) or account_id in self._unbound:
            return False
        remaining = self._limit_minutes(account_id, day) \
            - self.total_minutes(account_id, day)
        if remaining <= 1:
            self._reminders.add(key)
            return True
        return False

    def dump(self):
        return {
            "config": {
                "daily_limit_minutes": self.daily_limit_minutes,
                "holiday_limit_minutes": self.holiday_limit_minutes,
                "curfew": list(self.curfew),
                "compensation_cap_minutes": self.compensation_cap_minutes,
            },
            "intervals": self._intervals,
            "birth_years": self._birth_years,
            "holidays": sorted(self._holidays),
            "compensation": [
                {"account": account, "day": day, "minutes": minutes}
                for (account, day), minutes
                in sorted(self._compensation.items())],
            "batches": {
                batch: {"account": key[0], "day": key[1]}
                for batch, key in self._batches.items()},
            "unbound": sorted(self._unbound),
            "reminders": [list(key) for key in sorted(self._reminders)],
            "recovery": [
                {"account": account, "day": day, "entry": entry}
                for (account, day), entry in sorted(self._recovery.items())],
        }

    def load(self, state):
        config = state.get("config", {})
        self.daily_limit_minutes = config.get(
            "daily_limit_minutes", self.daily_limit_minutes)
        self.holiday_limit_minutes = config.get(
            "holiday_limit_minutes", self.holiday_limit_minutes)
        self.curfew = tuple(config.get("curfew", self.curfew))
        self.compensation_cap_minutes = config.get(
            "compensation_cap_minutes", self.compensation_cap_minutes)

        self._intervals = {
            account: [tuple(interval) for interval in intervals]
            for account, intervals in state.get("intervals", {}).items()}
        self._birth_years = dict(state.get("birth_years", {}))
        self._holidays = set(state.get("holidays", ()))
        self._compensation = {
            (item["account"], int(item["day"])): item["minutes"]
            for item in state.get("compensation", ())}
        self._batches = {
            batch: (item["account"], int(item["day"]))
            for batch, item in state.get("batches", {}).items()}
        self._unbound = set(state.get("unbound", ()))
        self._reminders = {
            tuple(key) for key in state.get("reminders", ())}
        self._recovery = {
            (item["account"], int(item["day"])): item["entry"]
            for item in state.get("recovery", ())}
