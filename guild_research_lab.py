class ResearchError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class GuildResearchLab:
    """Guild research slot, donations and atomic completion."""

    def __init__(self, wallet=None):
        self.wallet = wallet

    def set_level(self, guild_id, level):
        raise NotImplementedError

    def set_requirements(self, tech_id, required_points, prereq=None,
                         min_guild_level=1, duration=0, materials=0,
                         max_level=5):
        raise NotImplementedError

    def donate(self, guild_id, tech_id, points, txn_id=None):
        raise NotImplementedError

    def start_research(self, guild_id, tech_id, now=0):
        raise NotImplementedError

    def finish_research(self, guild_id, tech_id, now=0):
        raise NotImplementedError

    def cancel_research(self, guild_id, tech_id, now=0):
        raise NotImplementedError

    def research_slot(self, guild_id):
        raise NotImplementedError

    def tech_level(self, guild_id, tech_id):
        raise NotImplementedError

    def disband(self, guild_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
