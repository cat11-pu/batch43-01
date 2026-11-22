"""SCTP multi-homing association and path failover watch (skeleton)."""


class SctpError(Exception):
    """Association or path failure raised by the watch."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class SctpPathFailoverWatch:
    """Validates SCTP chunks and tracks multi-homed path state."""

    def __init__(self, state_path=None):
        self.state_path = state_path

    def add_path(self, peer, address, port=5000, verified=True, authenticated=True):
        raise NotImplementedError

    def receive(self, packet):
        raise NotImplementedError

    def failover(self, peer):
        raise NotImplementedError

    def primary(self, peer):
        raise NotImplementedError

    def paths(self, peer):
        raise NotImplementedError

    def send_seq(self, peer, path):
        raise NotImplementedError

    def checkpoint(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def associations(self):
        raise NotImplementedError
