class RepairError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class DurabilityRepairBench:
    """Item durability, repair pricing and transactional settlement."""

    def __init__(self, rate_per_point=10, wallet=None):
        self.rate_per_point = rate_per_point
        self.wallet = wallet

    def register_item(self, item_id, owner, max_durability, durability,
                      revision=1, set_id="default"):
        raise NotImplementedError

    def quote(self, item_id, discount=0.0):
        raise NotImplementedError

    def repair(self, item_id, actor, revision, discount=0.0, in_combat=False,
               now=0):
        raise NotImplementedError

    def apply_death_wear(self, item_id, amount=5):
        raise NotImplementedError

    def set_max_durability(self, item_id, new_max):
        raise NotImplementedError

    def repair_all(self, character, set_id=None):
        raise NotImplementedError

    def trade(self, item_id, new_owner):
        raise NotImplementedError

    def list_for_auction(self, item_id):
        raise NotImplementedError

    def attributes(self, item_id):
        raise NotImplementedError

    def snapshot(self, item_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
