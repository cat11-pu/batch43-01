"""Multi-issuer JWKS cache coordinator (skeleton)."""


class JwksError(Exception):
    """Configuration failure raised by the coordinator."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


class JwksCacheCoordinator:
    """Caches signing keys per issuer and coalesces refresh storms."""

    def __init__(self, fetcher=None, refresh_interval=30.0, cache_ttl=300.0):
        self.fetcher = fetcher
        self.refresh_interval = refresh_interval
        self.cache_ttl = cache_ttl

    def add_issuer(self, issuer, jwks_uri, keys=None):
        raise NotImplementedError

    def verify(self, token, now=0.0):
        raise NotImplementedError

    def fetch_count(self, issuer=None):
        raise NotImplementedError

    def cache_size(self, issuer):
        raise NotImplementedError
