class GatherError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class GatheringNodeRespawn:
    """Node occupancy, per-line cooldowns and settlement."""

    def __init__(self, cooldown=300, node_cap_per_line=10, inventory=None):
        self.cooldown = cooldown
        self.node_cap_per_line = node_cap_per_line
        self.inventory = inventory

    def configure_line(self, line_id, node_cap):
        raise NotImplementedError

    def spawn(self, node_id, line_id, item_id, position=(0, 0), now=0,
              rare=False, reachable=True):
        raise NotImplementedError

    def begin_gather(self, node_id, line_id, player_id, now=0):
        raise NotImplementedError

    def finish_gather(self, node_id, line_id, player_id, now=0, durability=1,
                      server_crit=0.0, client_crit=0.0, level=1,
                      required_level=1):
        raise NotImplementedError

    def interrupt_gather(self, node_id, line_id, player_id, now=0):
        raise NotImplementedError

    def availability(self, node_id, line_id, now=0):
        raise NotImplementedError

    def set_maintenance(self, now=0, duration=0):
        raise NotImplementedError

    def nodes(self, line_id):
        raise NotImplementedError

    def snapshot(self, player_id):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def load(self, state):
        raise NotImplementedError
