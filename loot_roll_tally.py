class LootError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class LootRollTally:
    """Per-drop need/greed roll state machine with delivery."""

    def __init__(self, mailer=None):
        self.mailer = mailer

    def open_roll(self, drop_id, item_id, mode="need_greed", deadline=0,
                  eligible=(), now=0):
        raise NotImplementedError

    def vote(self, drop_id, character, roll, kind="need", bonus=0, now=0):
        raise NotImplementedError

    def close_roll(self, drop_id, now=0):
        raise NotImplementedError

    def deliver(self, drop_id, character=None, free_slots=1):
        raise NotImplementedError

    def assign(self, drop_id, character):
        raise NotImplementedError

    def pause(self, drop_id):
        raise NotImplementedError

    def resume(self, drop_id, now=0):
        raise NotImplementedError

    def set_rule(self, rule):
        raise NotImplementedError

    def get_roll(self, drop_id):
        raise NotImplementedError

    def loot_item(self, drop_id, character):
        raise NotImplementedError

    def outbox(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
