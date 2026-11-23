class GatherError(ValueError):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


STATE_AVAILABLE = "available"
STATE_GATHERING = "gathering"
STATE_COOLDOWN = "cooldown"

CRIT_THRESHOLD = 0.5


class GatheringNodeRespawn:
    """Node occupancy, per-line cooldowns and settlement.

    A node is owned by exactly one player while being gathered. Finishing a
    gather settles item yield, tool durability and the task counter in one
    transaction: if the inventory grant fails, every side effect is rolled
    back and the node stays available. Cooldown deadlines are absolute
    timestamps tracked independently per line, so maintenance windows never
    shift them. Nodes only go live after passing the reachability check.
    """

    def __init__(self, cooldown=300, node_cap_per_line=10, inventory=None):
        self.cooldown = cooldown
        self.node_cap_per_line = node_cap_per_line
        self.inventory = inventory
        self._line_caps = {}
        self._nodes = {}
        self._players = {}
        self._maintenance = []

    def _key(self, node_id, line_id):
        return node_id, line_id

    def _get_node(self, node_id, line_id):
        node = self._nodes.get(self._key(node_id, line_id))
        if node is None:
            raise GatherError(
                "NODE_NOT_FOUND",
                {"node_id": node_id, "line_id": line_id},
            )
        return node

    def _ensure_player(self, player_id):
        player = self._players.get(player_id)
        if player is None:
            player = {
                "items": {},
                "durability_spent": 0,
                "tasks": {"gathering": 0},
            }
            self._players[player_id] = player
        return player

    @staticmethod
    def _release(node):
        node["state"] = STATE_AVAILABLE
        node["occupied_by"] = None
        node["cooldown_until"] = None

    def _line_node_count(self, line_id):
        return sum(
            1 for node in self._nodes.values() if node["line_id"] == line_id
        )

    def configure_line(self, line_id, node_cap):
        self._line_caps[line_id] = node_cap
        return {"line_id": line_id, "node_cap": node_cap}

    def spawn(self, node_id, line_id, item_id, position=(0, 0), now=0,
              rare=False, reachable=True):
        if not reachable:
            raise GatherError(
                "UNREACHABLE",
                {"node_id": node_id, "line_id": line_id,
                 "position": list(position)},
            )
        key = self._key(node_id, line_id)
        if key in self._nodes:
            raise GatherError(
                "NODE_EXISTS",
                {"node_id": node_id, "line_id": line_id},
            )
        position = tuple(position)
        for node in self._nodes.values():
            if node["line_id"] == line_id and node["position"] == position:
                raise GatherError(
                    "POSITION_OCCUPIED",
                    {"node_id": node["node_id"], "line_id": line_id,
                     "position": list(position)},
                )
        cap = self._line_caps.get(line_id, self.node_cap_per_line)
        if self._line_node_count(line_id) >= cap:
            raise GatherError(
                "LINE_NODE_LIMIT",
                {"line_id": line_id, "node_cap": cap},
            )
        node = {
            "node_id": node_id,
            "line_id": line_id,
            "item_id": item_id,
            "position": position,
            "rare": rare,
            "reachable": True,
            "state": STATE_AVAILABLE,
            "occupied_by": None,
            "cooldown_until": None,
            "spawned_at": now,
        }
        self._nodes[key] = node
        return dict(node)

    def begin_gather(self, node_id, line_id, player_id, now=0):
        node = self._get_node(node_id, line_id)
        if node["state"] == STATE_GATHERING:
            raise GatherError(
                "NODE_BUSY",
                {"node_id": node_id, "line_id": line_id,
                 "occupied_by": node["occupied_by"]},
            )
        if node["state"] == STATE_COOLDOWN:
            if now < node["cooldown_until"]:
                raise GatherError(
                    "NODE_COOLDOWN",
                    {"node_id": node_id, "line_id": line_id,
                     "available_at": node["cooldown_until"]},
                )
            self._release(node)
        self._ensure_player(player_id)
        node["state"] = STATE_GATHERING
        node["occupied_by"] = player_id
        return {
            "gathering": True,
            "node_id": node_id,
            "line_id": line_id,
            "player_id": player_id,
        }

    def finish_gather(self, node_id, line_id, player_id, now=0, durability=1,
                      server_crit=0.0, client_crit=0.0, level=1,
                      required_level=1):
        node = self._get_node(node_id, line_id)
        if node["state"] != STATE_GATHERING:
            raise GatherError(
                "NOT_GATHERING",
                {"node_id": node_id, "line_id": line_id},
            )
        if node["occupied_by"] != player_id:
            raise GatherError(
                "NODE_BUSY",
                {"node_id": node_id, "line_id": line_id,
                 "occupied_by": node["occupied_by"]},
            )
        if level < required_level:
            self._release(node)
            raise GatherError(
                "LEVEL_TOO_LOW",
                {"level": level, "required_level": required_level},
            )

        quantity = 2 if server_crit >= CRIT_THRESHOLD else 1
        item_id = node["item_id"]
        try:
            if self.inventory is not None:
                self.inventory(item_id, quantity)
            player = self._ensure_player(player_id)
            player["items"][item_id] = (
                player["items"].get(item_id, 0) + quantity
            )
            player["durability_spent"] += durability
            player["tasks"]["gathering"] += 1
        except Exception as exc:
            self._release(node)
            raise GatherError("GRANT_FAILED", str(exc)) from exc

        node["state"] = STATE_COOLDOWN
        node["occupied_by"] = None
        node["cooldown_until"] = now + self.cooldown
        return {
            "node_id": node_id,
            "line_id": line_id,
            "player_id": player_id,
            "item_id": item_id,
            "quantity": quantity,
            "crit": quantity > 1,
            "durability_spent": durability,
            "available_at": node["cooldown_until"],
        }

    def interrupt_gather(self, node_id, line_id, player_id, now=0):
        node = self._get_node(node_id, line_id)
        if node["state"] != STATE_GATHERING:
            raise GatherError(
                "NOT_GATHERING",
                {"node_id": node_id, "line_id": line_id},
            )
        if node["occupied_by"] != player_id:
            raise GatherError(
                "NODE_BUSY",
                {"node_id": node_id, "line_id": line_id,
                 "occupied_by": node["occupied_by"]},
            )
        self._release(node)
        return {
            "interrupted": True,
            "node_id": node_id,
            "line_id": line_id,
            "player_id": player_id,
        }

    def availability(self, node_id, line_id, now=0):
        node = self._get_node(node_id, line_id)
        if node["state"] == STATE_COOLDOWN and now >= node["cooldown_until"]:
            self._release(node)
        available = node["state"] == STATE_AVAILABLE
        return {
            "node_id": node_id,
            "line_id": line_id,
            "state": node["state"],
            "available": available,
            "visible": available,
            "available_at": node["cooldown_until"] if not available else now,
        }

    def set_maintenance(self, now=0, duration=0):
        window = {"start": now, "end": now + duration}
        self._maintenance.append(window)
        return dict(window)

    def nodes(self, line_id):
        return [
            dict(node)
            for node in self._nodes.values()
            if node["line_id"] == line_id
        ]

    def snapshot(self, player_id):
        player = self._players.get(player_id)
        if player is None:
            return {
                "player_id": player_id,
                "items": {},
                "durability_spent": 0,
                "tasks": {"gathering": 0},
            }
        return {
            "player_id": player_id,
            "items": dict(player["items"]),
            "durability_spent": player["durability_spent"],
            "tasks": dict(player["tasks"]),
        }

    def dump(self):
        return {
            "cooldown": self.cooldown,
            "node_cap_per_line": self.node_cap_per_line,
            "line_caps": dict(self._line_caps),
            "nodes": [
                dict(node, position=list(node["position"]))
                for node in self._nodes.values()
            ],
            "players": {
                player_id: {
                    "items": dict(player["items"]),
                    "durability_spent": player["durability_spent"],
                    "tasks": dict(player["tasks"]),
                }
                for player_id, player in self._players.items()
            },
            "maintenance": [dict(window) for window in self._maintenance],
        }

    def load(self, state):
        self.cooldown = state["cooldown"]
        self.node_cap_per_line = state["node_cap_per_line"]
        self._line_caps = dict(state["line_caps"])
        self._nodes = {}
        for node in state["nodes"]:
            record = dict(node)
            record["position"] = tuple(record["position"])
            self._nodes[self._key(record["node_id"], record["line_id"])] = (
                record
            )
        self._players = {
            player_id: {
                "items": dict(player["items"]),
                "durability_spent": player["durability_spent"],
                "tasks": dict(player["tasks"]),
            }
            for player_id, player in state["players"].items()
        }
        self._maintenance = [dict(window) for window in state["maintenance"]]
