"""GTP-U tunnel admission and replay arbitration (skeleton)."""


class GtpError(Exception):
    """Configuration or protocol failure raised by the arbiter."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class GtpUTunnelArbiter:
    """Validates GTP-U packets against active sessions before forwarding."""

    def __init__(self, replay_window=8, state_path=None):
        self.replay_window = replay_window
        self.state_path = state_path

    def add_session(self, teid, ue_ip, source_ip, charging_id=None):
        raise NotImplementedError

    def forward_uplink(
        self,
        teid,
        seq,
        src_ip=None,
        payload=b"",
        flow_label=None,
        fragment_index=0,
        version=1,
    ):
        raise NotImplementedError

    def forward_downlink(self, teid, dst_ip, payload=b""):
        raise NotImplementedError

    def handle_echo(self, src_ip, is_request=True):
        raise NotImplementedError

    def walk_extensions(self, headers):
        raise NotImplementedError

    def checkpoint(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
