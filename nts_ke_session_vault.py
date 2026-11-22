"""NTS-KE session vault and authenticated time sync (skeleton)."""


class NtsError(Exception):
    """Key-establishment or credential failure raised by the vault."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class NtsKeSessionVault:
    """Owns NTS-KE credentials and refuses unauthenticated time requests."""

    def __init__(self, allowed_algorithms=("AEAD_AES_SIV_CMAC_256",), state_path=None):
        self.allowed_algorithms = tuple(allowed_algorithms)
        self.state_path = state_path

    def establish(
        self,
        server,
        expected_host,
        cert_cn,
        algorithm="AEAD_AES_SIV_CMAC_256",
        nonce=None,
        lifetime=3600.0,
        now=0.0,
    ):
        raise NotImplementedError

    def sync(self, server, nonce, unique_id, now=0.0, server_time=None):
        raise NotImplementedError

    def credentials(self, server, now=0.0):
        raise NotImplementedError

    def poll(self, server, now=0.0):
        raise NotImplementedError

    def rotate_master_key(self, server):
        raise NotImplementedError

    def clock(self):
        raise NotImplementedError

    def counters(self):
        raise NotImplementedError
