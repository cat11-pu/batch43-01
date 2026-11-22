class TalentError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TalentRespecBinder:
    """Talent point ledger, scheme switching and respec fees."""

    def __init__(self, cost_per_level=100, switch_cooldown=10, wallet=None):
        self.cost_per_level = cost_per_level
        self.switch_cooldown = switch_cooldown
        self.wallet = wallet

    def grant_points(self, character, points, source="level", item=None):
        raise NotImplementedError

    def set_level(self, character, level):
        raise NotImplementedError

    def spend_point(self, character, node, points=1):
        raise NotImplementedError

    def respec(self, character, level=None):
        raise NotImplementedError

    def save_scheme(self, character, scheme_id, allocation, revision,
                    base_revision=0):
        raise NotImplementedError

    def activate_scheme(self, character, scheme_id, now=0):
        raise NotImplementedError

    def import_scheme(self, character, scheme_id, payload):
        raise NotImplementedError

    def equip_bonus(self, character, item_id, points):
        raise NotImplementedError

    def unequip_bonus(self, character, item_id):
        raise NotImplementedError

    def preview(self, character, scheme_id):
        raise NotImplementedError

    def snapshot(self, character):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
