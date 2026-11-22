class TribunalError(ValueError):
    """Raised when a tribunal operation is rejected; ``code`` is machine readable."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReportAbuseTribunal:
    """Abuse report intake, merging, review, penalties, appeals and evidence."""

    def __init__(self, merge_window=300, cooldown=60, ban_duration=3600):
        self.merge_window = merge_window
        self.cooldown = cooldown
        self.ban_duration = ban_duration

    def file_report(self, report_id, reporter_id, target_id, reason, now=0,
                    evidence=None, match_id=None, server=None,
                    target_server=None, device_id=None, category="text",
                    audio=None):
        raise NotImplementedError

    def withdraw(self, ticket_id, reporter_id, now=0):
        raise NotImplementedError

    def review(self, ticket_id, reviewer, decision, reason=None, now=0):
        raise NotImplementedError

    def appeal(self, ban_id, reviewer, granted, now=0, reason=None):
        raise NotImplementedError

    def tick(self, now=0):
        raise NotImplementedError

    def rename(self, account_id, new_name):
        raise NotImplementedError

    def store_replay(self, match_id, payload):
        raise NotImplementedError

    def replay(self, match_id):
        raise NotImplementedError

    def ticket(self, ticket_id):
        raise NotImplementedError

    def tickets(self, target_id=None):
        raise NotImplementedError

    def evidence(self, ticket_id):
        raise NotImplementedError

    def bans(self, account_id=None):
        raise NotImplementedError

    def account(self, account_id):
        raise NotImplementedError

    def audit(self):
        raise NotImplementedError
