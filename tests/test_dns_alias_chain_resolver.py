from concurrent.futures import ThreadPoolExecutor

import dns_alias_chain_resolver


def _resolver(**kwargs):
    return dns_alias_chain_resolver.DnsAliasChainResolver(**kwargs)


def test_cname_loop_returns_servfail_without_partial_answer():
    resolver = _resolver()
    resolver.add_record("a.example.com", "CNAME", "b.example.com")
    resolver.add_record("b.example.com", "CNAME", "a.example.com")
    result = resolver.resolve("a.example.com")
    assert result["status"] == "SERVFAIL"
    assert result["error"] == "CNAME_LOOP"
    assert result["answers"] == []


def test_chain_deeper_than_limit_returns_servfail():
    resolver = _resolver(max_depth=16)
    for index in range(18):
        resolver.add_record("n%d.example.com" % index, "CNAME", "n%d.example.com" % (index + 1))
    resolver.add_record("n18.example.com", "A", "192.0.2.18")
    result = resolver.resolve("n0.example.com")
    assert result["status"] == "SERVFAIL"
    assert result["error"] == "CNAME_CHAIN_TOO_LONG"


def test_valid_chain_returns_final_address_and_minimum_ttl():
    resolver = _resolver()
    resolver.add_record("www.example.com", "CNAME", "edge.example.com", ttl=300)
    resolver.add_record("edge.example.com", "CNAME", "origin.example.com", ttl=120)
    resolver.add_record("origin.example.com", "A", "192.0.2.10", ttl=30)
    result = resolver.resolve("www.example.com")
    assert result["status"] == "NOERROR"
    assert ("origin.example.com", "A", "192.0.2.10") in result["answers"]
    assert result["ttl"] == 30


def test_alias_to_out_of_zone_target_is_not_trusted_or_cached():
    resolver = _resolver(allowed_zones=("example.com",))
    resolver.add_record("www.example.com", "CNAME", "evil.test")
    resolver.add_record("evil.test", "A", "203.0.113.9")
    result = resolver.resolve("www.example.com")
    assert result["status"] == "SERVFAIL"
    assert result["error"] == "OUT_OF_ZONE"
    assert resolver.cache_lookup("evil.test") is None


def test_cname_and_other_data_at_same_name_conflict():
    resolver = _resolver()
    resolver.add_record("www.example.com", "CNAME", "edge.example.com")
    resolver.add_record("www.example.com", "A", "192.0.2.11")
    resolver.add_record("edge.example.com", "A", "192.0.2.12")
    result = resolver.resolve("www.example.com")
    assert result["status"] == "SERVFAIL"
    assert result["error"] == "CNAME_CONFLICT"


def test_failed_target_is_not_negative_cached_under_alias_name():
    resolver = _resolver()
    resolver.add_record("www.example.com", "CNAME", "missing.example.com")
    result = resolver.resolve("www.example.com")
    assert result["status"] == "NXDOMAIN"
    assert resolver.cache_lookup("www.example.com") is None
    target_cache = resolver.cache_lookup("missing.example.com")
    assert target_cache is not None
    assert target_cache["status"] == "NXDOMAIN"


def test_names_are_matched_case_insensitively():
    resolver = _resolver()
    resolver.add_record("WWW.Example.COM", "A", "192.0.2.20")
    result = resolver.resolve("www.eXample.com")
    assert result["status"] == "NOERROR"
    assert ("www.example.com", "A", "192.0.2.20") in result["answers"]


def test_concurrent_queries_for_same_name_issue_one_upstream_lookup():
    resolver = _resolver()
    resolver.add_record("www.example.com", "A", "192.0.2.21")

    def query(_):
        return resolver.resolve("www.example.com")

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(query, range(5)))
    assert all(item["status"] == "NOERROR" for item in results)
    assert resolver.upstream_count() == 1


def test_large_response_sets_truncated_bit():
    resolver = _resolver(udp_size=512)
    for index in range(40):
        resolver.add_record("many.example.com", "A", "192.0.2.%d" % (100 + index))
    result = resolver.resolve("many.example.com")
    assert result["truncated"] is True


def test_chain_ttl_controls_cache_lifetime():
    resolver = _resolver()
    resolver.add_record("www.example.com", "CNAME", "edge.example.com", ttl=300)
    resolver.add_record("edge.example.com", "A", "192.0.2.30", ttl=30)
    resolver.resolve("www.example.com", now=0.0)
    resolver.resolve("www.example.com", now=10.0)
    assert resolver.upstream_count() == 1
    resolver.resolve("www.example.com", now=31.0)
    assert resolver.upstream_count() == 2


def test_target_class_mismatch_is_rejected():
    resolver = _resolver()
    resolver.add_record("www.example.com", "CNAME", "edge.example.com", rclass="IN")
    resolver.add_record("edge.example.com", "A", "192.0.2.31", rclass="CH")
    result = resolver.resolve("www.example.com")
    assert result["status"] == "SERVFAIL"
    assert result["error"] == "CLASS_MISMATCH"


def test_authoritative_update_invalidates_cached_answer():
    resolver = _resolver()
    resolver.add_record("www.example.com", "A", "192.0.2.40")
    first = resolver.resolve("www.example.com", now=0.0)
    assert ("www.example.com", "A", "192.0.2.40") in first["answers"]
    resolver.add_record("www.example.com", "A", "192.0.2.41", replace=True)
    second = resolver.resolve("www.example.com", now=1.0)
    assert ("www.example.com", "A", "192.0.2.41") in second["answers"]
    assert ("www.example.com", "A", "192.0.2.40") not in second["answers"]


def test_overlong_alias_target_name_rejected():
    resolver = _resolver()
    resolver.add_record("www.example.com", "CNAME", "a" * 300 + ".example.com")
    result = resolver.resolve("www.example.com")
    assert result["status"] == "SERVFAIL"
    assert result["error"] == "NAME_TOO_LONG"


def test_loop_detection_is_case_insensitive():
    resolver = _resolver()
    resolver.add_record("a.example.com", "CNAME", "B.example.com")
    resolver.add_record("b.example.com", "CNAME", "A.EXAMPLE.COM")
    result = resolver.resolve("a.example.com")
    assert result["status"] == "SERVFAIL"
    assert result["error"] == "CNAME_LOOP"


def test_multiple_address_records_are_all_returned():
    resolver = _resolver()
    resolver.add_record("pool.example.com", "A", "192.0.2.50")
    resolver.add_record("pool.example.com", "A", "192.0.2.51")
    result = resolver.resolve("pool.example.com")
    assert result["status"] == "NOERROR"
    assert len(result["answers"]) == 2
