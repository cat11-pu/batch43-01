"""CIBA consent transcript and single-token issuance (skeleton)."""


class CibaError(Exception):
    """Authentication-request failure raised by the transcript."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class CibaConsentTranscript:
    """Runs the CIBA request state machine with poll and callback delivery."""

    def __init__(self, poll_interval=5.0, request_ttl=600.0, state_path=None):
        self.poll_interval = poll_interval
        self.request_ttl = request_ttl
        self.state_path = state_path

    def start(self, request_id, client_id, scope="openid", ttl=None, now=0.0):
        raise NotImplementedError

    def poll(self, request_id, client_id, now=0.0):
        raise NotImplementedError

    def callback(self, request_id, client_id, source_verified=True, now=0.0):
        raise NotImplementedError

    def approve(self, request_id, user="alice", now=0.0):
        raise NotImplementedError

    def deny(self, request_id, now=0.0):
        raise NotImplementedError

    def records(self):
        raise NotImplementedError
