"""Multi-issuer JWKS cache coordinator."""

import threading
from urllib.parse import urlparse


class JwksError(Exception):
    """Configuration failure raised by the coordinator."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or code)


_ALG_FAMILY = {
    "HS": "oct",
    "RS": "RSA",
    "PS": "RSA",
    "ES": "EC",
}


class JwksCacheCoordinator:
    """Caches signing keys per issuer and coalesces refresh storms.

    Keys are isolated per (issuer, kid): a key fetched from one issuer is
    never used to verify another issuer's token. Unknown kids trigger at
    most one refresh per issuer per ``refresh_interval``; concurrent
    requests are coalesced by a lock so a storm causes a single fetch.
    """

    def __init__(self, fetcher=None, refresh_interval=30.0, cache_ttl=300.0):
        self.fetcher = fetcher
        self.refresh_interval = refresh_interval
        self.cache_ttl = cache_ttl
        self._lock = threading.RLock()
        self._issuers = {}
        self._fetch_counts = {}

    def add_issuer(self, issuer, jwks_uri, keys=None):
        with self._lock:
            self._issuers[issuer] = {
                "jwks_uri": jwks_uri,
                "keys": {},
                "last_refresh": None,
            }
            for key in keys or []:
                kid = key.get("kid")
                if kid is not None:
                    self._issuers[issuer]["keys"][kid] = {
                        "key": key,
                        "fetched_at": 0.0,
                    }
            self._fetch_counts.setdefault(issuer, 0)

    def verify(self, token, now=0.0):
        issuer = token.get("iss")
        kid = token.get("kid")
        with self._lock:
            entry = self._issuers.get(issuer)
            if entry is None:
                return self._error("JWKS_UNKNOWN_ISSUER", kid)
            cached = entry["keys"].get(kid)
            if cached is not None and now - cached["fetched_at"] < self.cache_ttl:
                return self._check(cached, token, kid)
            return self._handle_miss(issuer, entry, cached, token, kid, now)

    def fetch_count(self, issuer=None):
        with self._lock:
            if issuer is not None:
                return self._fetch_counts.get(issuer, 0)
            return sum(self._fetch_counts.values())

    def cache_size(self, issuer):
        with self._lock:
            entry = self._issuers.get(issuer)
            return len(entry["keys"]) if entry is not None else 0

    def _handle_miss(self, issuer, entry, cached, token, kid, now):
        if not self._uri_secure(entry["jwks_uri"]):
            if cached is not None:
                return self._check(cached, token, kid)
            return self._error("JWKS_URI_INSECURE", kid)
        if self._refresh_allowed(entry, now):
            entry["last_refresh"] = now
            self._fetch_counts[issuer] = self._fetch_counts.get(issuer, 0) + 1
            try:
                fresh = self.fetcher(issuer) if self.fetcher is not None else None
            except Exception:
                fresh = None
            new_keys = {}
            for key in fresh or []:
                fresh_kid = key.get("kid")
                if fresh_kid is not None:
                    new_keys[fresh_kid] = {"key": key, "fetched_at": now}
            if new_keys:
                # A non-empty key set atomically replaces the cache; keys
                # dropped upstream stop verifying immediately. The request
                # that triggered the refresh fails closed so it is answered
                # from a consistent snapshot; a retry uses the new keys.
                entry["keys"] = new_keys
                return self._error("JWKS_UNKNOWN_KID", kid)
            # Empty or failed upstream response never wipes a good cache;
            # renew the stale entry so the window stays consistent.
            if cached is not None:
                cached["fetched_at"] = now
                return self._check(cached, token, kid)
            return self._error("JWKS_UNKNOWN_KID", kid)
        if cached is not None:
            return self._check(cached, token, kid)
        return self._error("JWKS_UNKNOWN_KID", kid)

    def _refresh_allowed(self, entry, now):
        last = entry["last_refresh"]
        return last is None or now - last >= self.refresh_interval

    def _check(self, cached, token, kid):
        key = cached["key"]
        use = key.get("use")
        if use is not None and use != "sig":
            return self._error("JWKS_KEY_USE", kid)
        token_alg = token.get("alg")
        key_alg = key.get("alg")
        if key_alg is not None and token_alg is not None and key_alg != token_alg:
            return self._error("JWKS_ALG_MISMATCH", kid)
        if not self._kty_matches(key.get("kty"), token_alg):
            return self._error("JWKS_ALG_MISMATCH", kid)
        if token.get("signature_key") != key.get("key"):
            return self._error("JWKS_SIGNATURE_INVALID", kid)
        return {
            "valid": True,
            "code": None,
            "kid": kid,
            "fetched_at": cached["fetched_at"],
        }

    @staticmethod
    def _kty_matches(kty, alg):
        if not kty or not alg:
            return True
        if alg == "EdDSA":
            return kty == "OKP"
        expected = _ALG_FAMILY.get(alg[:2])
        if expected is None:
            return True
        return kty == expected

    @staticmethod
    def _uri_secure(uri):
        try:
            return urlparse(uri).scheme == "https"
        except Exception:
            return False

    @staticmethod
    def _error(code, kid):
        return {"valid": False, "code": code, "kid": kid, "fetched_at": None}
