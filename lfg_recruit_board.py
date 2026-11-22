class LfgError(ValueError):
    """Raised when a recruitment listing or application is rejected."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class LfgRecruitBoard:
    """Recruitment listings, applications and atomic roster slots."""

    def __init__(self, capacity=5, application_ttl=300):
        self.capacity = capacity
        self.application_ttl = application_ttl

    def create(self, listing_id, leader, capacity=None, requirements=None,
               now=0, region="r1", title="", pinned=False, dungeon=None):
        raise NotImplementedError

    def update(self, listing_id, actor, requirements=None, title=None,
               pinned=None, now=0):
        raise NotImplementedError

    def apply(self, application_id, listing_id, account, character=None,
              role=None, now=0, lockouts=(), region="r1"):
        raise NotImplementedError

    def accept(self, application_id, actor, now=0):
        raise NotImplementedError

    def reject(self, application_id, actor, now=0):
        raise NotImplementedError

    def expire(self, application_id, now=0):
        raise NotImplementedError

    def cancel(self, listing_id, actor, now=0):
        raise NotImplementedError

    def get_listing(self, listing_id):
        raise NotImplementedError

    def applications(self, listing_id=None):
        raise NotImplementedError

    def members(self, listing_id):
        raise NotImplementedError

    def listings(self, region=None, now=0):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
