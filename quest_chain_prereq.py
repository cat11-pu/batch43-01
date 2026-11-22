class QuestError(ValueError):
    """Raised when a quest operation violates chain or inventory rules."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class QuestChainPrereq:
    """Quest prerequisite graph, per-character instances and rewards."""

    def __init__(self):
        pass

    def define_quest(self, quest_id, prereqs=None, faction=None, objective=None,
                     items_required=None, rewards=None, grant_on_accept=None,
                     aliases=None):
        raise NotImplementedError

    def accept(self, character, quest_id, now=0):
        raise NotImplementedError

    def progress(self, character, quest_id, amount=1, objective=None,
                 party=None, now=0):
        raise NotImplementedError

    def complete(self, character, quest_id, now=0):
        raise NotImplementedError

    def abandon(self, character, quest_id, now=0):
        raise NotImplementedError

    def fail(self, character, quest_id, now=0):
        raise NotImplementedError

    def add_item(self, character, item_id, count=1):
        raise NotImplementedError

    def transfer_item(self, character, target_character, item_id, count=1):
        raise NotImplementedError

    def inventory(self, character):
        raise NotImplementedError

    def quest_state(self, character, quest_id):
        raise NotImplementedError

    def tracker(self, character):
        raise NotImplementedError

    def mailbox(self, character):
        raise NotImplementedError

    def import_completion(self, character, quest_ids):
        raise NotImplementedError

    def set_faction(self, character, faction):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
