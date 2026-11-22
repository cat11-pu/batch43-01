from concurrent.futures import ThreadPoolExecutor

import jwks_cache_coordinator


ISSUER = "https://idp.example"


def _key(kid="k1", alg="RS256", key="key-1", use="sig", kty="RSA"):
    return {"kid": kid, "alg": alg, "key": key, "use": use, "kty": kty}


def _token(kid="k1", alg="RS256", signature_key="key-1", issuer=ISSUER):
    return {"iss": issuer, "kid": kid, "alg": alg, "signature_key": signature_key}


class _Fetcher:
    def __init__(self, keys):
        self.keys = keys
        self.calls = []

    def __call__(self, issuer):
        self.calls.append(issuer)
        return list(self.keys)


def test_unknown_kid_triggers_single_refresh_per_interval():
    fetcher = _Fetcher([_key()])
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator(fetcher=fetcher)
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks")
    first = coordinator.verify(_token(kid="missing"), now=0.0)
    second = coordinator.verify(_token(kid="missing"), now=1.0)
    assert first["code"] == "JWKS_UNKNOWN_KID"
    assert second["code"] == "JWKS_UNKNOWN_KID"
    assert len(fetcher.calls) == 1


def test_refresh_allowed_again_after_interval():
    fetcher = _Fetcher([_key()])
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator(fetcher=fetcher)
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks")
    coordinator.verify(_token(kid="missing"), now=0.0)
    coordinator.verify(_token(kid="missing"), now=31.0)
    assert len(fetcher.calls) == 2


def test_concurrent_unknown_kid_requests_coalesce():
    fetcher = _Fetcher([_key()])
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator(fetcher=fetcher)
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks")

    def query(_):
        return coordinator.verify(_token(kid="missing"), now=0.0)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(query, range(5)))
    assert all(item["code"] == "JWKS_UNKNOWN_KID" for item in results)
    assert len(fetcher.calls) == 1


def test_algorithm_mismatch_rejected():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key(alg="RS256")])
    result = coordinator.verify(_token(alg="HS256"), now=0.0)
    assert result["valid"] is False
    assert result["code"] == "JWKS_ALG_MISMATCH"


def test_same_kid_under_different_issuers_is_isolated():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key(key="key-a")])
    coordinator.add_issuer("https://other.example", "https://other.example/jwks", keys=[_key(key="key-b")])
    wrong = coordinator.verify(_token(signature_key="key-a"), now=0.0)
    other = coordinator.verify(
        _token(issuer="https://other.example", signature_key="key-b"), now=0.0
    )
    assert wrong["valid"] is True
    assert other["valid"] is True
    cross = coordinator.verify(
        _token(issuer="https://other.example", signature_key="key-a"), now=0.0
    )
    assert cross["code"] == "JWKS_SIGNATURE_INVALID"


def test_encryption_only_key_is_not_used_for_signatures():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key(use="enc")])
    result = coordinator.verify(_token(), now=0.0)
    assert result["valid"] is False
    assert result["code"] == "JWKS_KEY_USE"


def test_plaintext_jwks_uri_is_refused():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator(fetcher=_Fetcher([_key()]))
    coordinator.add_issuer(ISSUER, "http://idp.example/jwks")
    result = coordinator.verify(_token(), now=0.0)
    assert result["valid"] is False
    assert result["code"] == "JWKS_URI_INSECURE"


def test_deleted_key_invalid_after_cache_expiry():
    fetcher = _Fetcher([_key(key="new-key")])
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator(fetcher=fetcher, cache_ttl=10.0)
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key(key="old-key")])
    assert coordinator.verify(_token(signature_key="old-key"), now=0.0)["valid"] is True
    stale = coordinator.verify(_token(signature_key="old-key"), now=11.0)
    assert stale["valid"] is False
    assert stale["code"] == "JWKS_UNKNOWN_KID"


def test_rotation_overlap_accepts_old_and_new_keys():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    coordinator.add_issuer(
        ISSUER,
        "https://idp.example/jwks",
        keys=[_key(kid="old", key="old-key"), _key(kid="new", key="new-key")],
    )
    assert coordinator.verify(_token(kid="old", signature_key="old-key"), now=0.0)["valid"] is True
    assert coordinator.verify(_token(kid="new", signature_key="new-key"), now=0.0)["valid"] is True


def test_empty_upstream_response_does_not_wipe_cache():
    fetcher = _Fetcher([])
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator(fetcher=fetcher, cache_ttl=10.0)
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key(key="cached-key")])
    result = coordinator.verify(_token(signature_key="cached-key"), now=11.0)
    assert result["valid"] is True


def test_unknown_issuer_is_rejected():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    result = coordinator.verify(_token(issuer="https://unknown.example"), now=0.0)
    assert result["valid"] is False
    assert result["code"] == "JWKS_UNKNOWN_ISSUER"


def test_signature_key_mismatch_is_rejected():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key(key="correct-key")])
    result = coordinator.verify(_token(signature_key="wrong-key"), now=0.0)
    assert result["valid"] is False
    assert result["code"] == "JWKS_SIGNATURE_INVALID"


def test_valid_token_is_accepted_with_key_id():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key()])
    result = coordinator.verify(_token(), now=0.0)
    assert result["valid"] is True
    assert result["kid"] == "k1"
    assert result["code"] is None


def test_unknown_kid_refresh_does_not_drop_cached_keys():
    fetcher = _Fetcher([_key()])
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator(fetcher=fetcher)
    coordinator.add_issuer(ISSUER, "https://idp.example/jwks", keys=[_key()])
    assert coordinator.verify(_token(), now=0.0)["valid"] is True
    coordinator.verify(_token(kid="missing"), now=1.0)
    assert coordinator.cache_size(ISSUER) >= 1
    assert coordinator.verify(_token(), now=2.0)["valid"] is True


def test_rsa_public_key_cannot_be_used_as_hmac_secret():
    coordinator = jwks_cache_coordinator.JwksCacheCoordinator()
    coordinator.add_issuer(
        ISSUER,
        "https://idp.example/jwks",
        keys=[{"kid": "k1", "alg": "RS256", "key": "public", "use": "sig", "kty": "RSA"}],
    )
    result = coordinator.verify(_token(alg="HS256"), now=0.0)
    assert result["valid"] is False
    assert result["code"] == "JWKS_ALG_MISMATCH"
