import pytest

import dhcpv6_pd_broker


def _broker(**kwargs):
    return dhcpv6_pd_broker.Dhcpv6PdBroker(**kwargs)


def test_same_device_and_iaid_receives_single_binding():
    broker = _broker()
    first = broker.request("00:01:02", 1, now=0.0)
    second = broker.request("00:01:02", 1, now=1.0)
    assert first["prefixes"] == second["prefixes"]
    assert broker.usage(now=1.0) == 1


def test_renew_after_release_returns_nobinding():
    broker = _broker()
    broker.request("00:01:02", 1)
    broker.release("00:01:02", 1)
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.renew("00:01:02", 1)
    assert exc.value.code == "NoBinding"


def test_duplicate_release_does_not_double_decrement_pool():
    broker = _broker()
    broker.request("00:01:02", 1)
    broker.release("00:01:02", 1)
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.release("00:01:02", 1)
    assert exc.value.code == "NoBinding"
    assert broker.usage() == 0


def test_restart_restores_leases_and_does_not_reissue(tmp_path):
    state = str(tmp_path / "pd-state.json")
    first = _broker(state_path=state)
    original = first.request("00:01:02", 1, now=0.0)["prefixes"]
    restarted = _broker(state_path=state)
    other = restarted.request("00:0a:0b", 1, now=1.0)["prefixes"]
    assert original != other
    assert restarted.renew("00:01:02", 1, now=1.0)["prefixes"] == original


def test_expired_lease_not_reclaimed_before_valid_lifetime():
    broker = _broker(valid_lifetime=10, capacity=1)
    broker.request("00:01:02", 1, now=0.0)
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.request("00:03:04", 1, now=5.0)
    assert exc.value.code == "NoPrefixAvailable"


def test_expired_lease_reclaimed_after_valid_lifetime():
    broker = _broker(valid_lifetime=10, capacity=1)
    first = broker.request("00:01:02", 1, now=0.0)["prefixes"]
    second = broker.request("00:03:04", 1, now=10.5)["prefixes"]
    assert second == first


def test_prefix_length_outside_allowed_range_rejected():
    broker = _broker()
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.request("00:01:02", 1, prefix_length=40)
    assert exc.value.code == "InvalidPrefixLength"
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc2:
        broker.request("00:01:02", 1, prefix_length=65)
    assert exc2.value.code == "InvalidPrefixLength"


def test_multiple_requested_prefixes_are_all_allocated():
    broker = _broker()
    result = broker.request("00:01:02", 1, count=2)
    assert len(result["prefixes"]) == 2
    assert broker.usage() == 2


def test_relay_hop_limit_exceeded_rejected():
    broker = _broker(max_hops=2)
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.request("00:01:02", 1, hop_count=3)
    assert exc.value.code == "HopLimitExceeded"
    assert broker.usage() == 0


def test_duid_formats_are_normalized_to_one_identity():
    broker = _broker()
    first = broker.request("00:01:0A", 1)["prefixes"]
    second = broker.request("00010a", 1)["prefixes"]
    assert second == first
    assert broker.usage() == 1


def test_rapid_commit_and_four_step_do_not_double_allocate():
    broker = _broker()
    first = broker.request("00:01:02", 1, rapid_commit=True, now=0.0)["prefixes"]
    second = broker.request("00:01:02", 1, rapid_commit=False, now=1.0)["prefixes"]
    assert second == first
    assert broker.usage(now=1.0) == 1


def test_renew_lifetimes_decrease_with_elapsed_time():
    broker = _broker(valid_lifetime=100, preferred_lifetime=50)
    broker.request("00:01:02", 1, now=0.0)
    renewed = broker.renew("00:01:02", 1, now=40.0)
    assert renewed["valid_lifetime"] == 60
    assert renewed["preferred_lifetime"] == 10


def test_same_device_different_iaid_gets_distinct_prefixes():
    broker = _broker()
    first = broker.request("00:01:02", 1)["prefixes"]
    second = broker.request("00:01:02", 2)["prefixes"]
    assert first != second
    assert broker.usage() == 2


def test_relay_link_change_does_not_steal_or_duplicate_binding():
    broker = _broker()
    first = broker.request("00:01:02", 1, link="relay-a")["prefixes"]
    second = broker.request("00:01:02", 1, link="relay-b")["prefixes"]
    assert second == first
    assert broker.usage() == 1


def test_released_prefix_becomes_available_again():
    broker = _broker(capacity=1)
    first = broker.request("00:01:02", 1)["prefixes"]
    broker.release("00:01:02", 1)
    second = broker.request("00:03:04", 1)["prefixes"]
    assert second == first


def test_concurrent_requests_for_same_binding_allocate_once():
    broker = _broker()
    barrier = __import__("threading").Barrier(4)
    results = []

    def worker():
        barrier.wait()
        results.append(broker.request("00:01:02", 1, now=0.0))

    threads = [__import__("threading").Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 4
    assert all(item["prefixes"] == results[0]["prefixes"] for item in results)
    assert broker.usage() == 1


def test_concurrent_renew_and_release_leaves_no_orphan_prefixes():
    broker = _broker()
    broker.request("00:01:02", 1, now=0.0)

    def renewer():
        for _ in range(50):
            try:
                broker.renew("00:01:02", 1, now=1.0)
            except dhcpv6_pd_broker.Dhcpv6Error as error:
                assert error.code == "NoBinding"

    def releaser():
        for _ in range(50):
            try:
                broker.release("00:01:02", 1, now=1.0)
            except dhcpv6_pd_broker.Dhcpv6Error as error:
                assert error.code == "NoBinding"

    threads = [
        __import__("threading").Thread(target=renewer),
        __import__("threading").Thread(target=releaser),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    tracked = {
        prefix
        for binding in broker._bindings.values()
        for prefix in binding["prefixes"]
    }
    assert broker._allocated == tracked
    assert broker.usage(now=1.0) == len(tracked)


def test_prefix_length_boundaries_are_enforced():
    broker = _broker()
    assert len(broker.request("00:01:02", 1, prefix_length=48)["prefixes"]) == 1
    assert len(broker.request("00:01:03", 1, prefix_length=64)["prefixes"]) == 1
    for bad_length in (47, 66, 72):
        with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
            broker.request("00:01:04", 1, prefix_length=bad_length)
        assert exc.value.code == "InvalidPrefixLength"


def test_hop_count_at_limit_is_accepted_and_over_limit_rejected():
    broker = _broker(max_hops=2)
    assert broker.request("00:01:02", 1, hop_count=2)["prefixes"]
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.request("00:01:03", 1, hop_count=3)
    assert exc.value.code == "HopLimitExceeded"


def test_renew_expired_lease_returns_nobinding():
    broker = _broker(valid_lifetime=10)
    broker.request("00:01:02", 1, now=0.0)
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.renew("00:01:02", 1, now=10.0)
    assert exc.value.code == "NoBinding"


def test_failed_multi_prefix_request_is_atomic():
    broker = _broker(capacity=1)
    with pytest.raises(dhcpv6_pd_broker.Dhcpv6Error) as exc:
        broker.request("00:01:02", 1, count=2)
    assert exc.value.code == "NoPrefixAvailable"
    assert broker.usage() == 0
    assert broker._allocated == set()
