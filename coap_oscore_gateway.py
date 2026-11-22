"""CoAP / OSCORE gateway replay, block and observation guard (skeleton)."""


class CoapError(Exception):
    """Protocol failure raised by the gateway."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class CoapOscoreGateway:
    """Terminates OSCORE requests and enforces replay and block assembly rules."""

    def __init__(
        self,
        state_path=None,
        replay_window=16,
        max_token_len=8,
        group_key_id=None,
        max_skew=300,
    ):
        self.state_path = state_path
        self.replay_window = replay_window
        self.max_token_len = max_token_len
        self.group_key_id = group_key_id
        self.max_skew = max_skew

    def handle_request(
        self,
        endpoint,
        sender_id,
        partial_iv,
        token=b"t",
        content_format=0,
        multicast=False,
        group_key_id=None,
        timestamp=0.0,
        now=0.0,
        payload=b"",
    ):
        raise NotImplementedError

    def begin_block(self, endpoint, token, content_format=0):
        raise NotImplementedError

    def add_block(self, buffer_id, payload, content_format=None):
        raise NotImplementedError

    def observe(self, endpoint, token, resource):
        raise NotImplementedError

    def cancel_observe(self, token):
        raise NotImplementedError

    def reset(self, endpoint, token):
        raise NotImplementedError

    def notify(self, token, payload):
        raise NotImplementedError

    def notifications(self, token):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
