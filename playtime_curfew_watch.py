class PlaytimeError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class PlaytimeCurfewWatch:
    """Account-level playtime accumulation, curfew and compensation."""

    def __init__(self, daily_limit_minutes=90, holiday_limit_minutes=180,
                 curfew=(22, 8), compensation_cap_minutes=120):
        self.daily_limit_minutes = daily_limit_minutes
        self.holiday_limit_minutes = holiday_limit_minutes
        self.curfew = curfew
        self.compensation_cap_minutes = compensation_cap_minutes

    def record_session(self, account_id, device_id, seconds, now,
                       afk_seconds=0):
        raise NotImplementedError

    def total_minutes(self, account_id, day):
        raise NotImplementedError

    def check(self, account_id, now, client_tz_offset=0, foreign_minutes=0,
              current_year=2026):
        raise NotImplementedError

    def set_birthdate(self, account_id, year):
        raise NotImplementedError

    def set_holiday(self, day, is_holiday=True):
        raise NotImplementedError

    def compensate(self, account_id, minutes, batch_id, day):
        raise NotImplementedError

    def set_parental_unbind(self, account_id, value=True):
        raise NotImplementedError

    def link_guest(self, guest_id, account_id, now):
        raise NotImplementedError

    def force_offline(self, account_id, now):
        raise NotImplementedError

    def reminder_due(self, account_id, now):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
