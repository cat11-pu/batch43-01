import pytest

import bgp_origin_validator


def _roi(prefix="10.0.0.0/8", max_length=24, origin_as=65001, updated_at=1):
    return {
        "prefix": prefix,
        "max_length": max_length,
        "origin_as": origin_as,
        "updated_at": updated_at,
    }


def test_invalid_origin_is_not_installed():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    result = validator.install_route("10.1.0.0/16", 65002, (65002, 65001))
    assert result["installed"] is False
    assert result["state"] == "invalid"
    assert result["code"] == "BGP_ROV_INVALID"
    assert validator.advertise() == []


def test_invalid_route_is_never_advertised():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    validator.install_route("10.1.0.0/16", 65002)
    assert "10.1.0.0/16" not in validator.advertise()


def test_invalid_route_cannot_override_installed_valid_route():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    assert validator.install_route("10.1.0.0/16", 65001, local_pref=50)["installed"] is True
    assert validator.install_route("10.1.0.0/16", 65002, local_pref=500)["installed"] is False
    assert validator.rov_state("10.1.0.0/16") == "valid"
    assert validator.advertise() == ["10.1.0.0/16"]


def test_prefix_longer_than_max_length_is_invalid():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi(max_length=24)])
    result = validator.validate("10.1.2.0/25", 65001)
    assert result["state"] == "invalid"
    assert result["code"] == "BGP_ROV_INVALID"


def test_prefix_at_max_length_boundary_is_valid():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi(max_length=24)])
    assert validator.validate("10.1.2.0/24", 65001)["state"] == "valid"


def test_as_loop_in_middle_of_path_is_invalid():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    result = validator.validate("10.1.0.0/16", 65001, (65001, 65002, 65001))
    assert result["state"] == "invalid"
    assert result["code"] == "BGP_ROV_INVALID"


def test_withdraw_clears_invalid_state_before_reannounce():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    validator.install_route("10.1.0.0/16", 65002)
    assert validator.rov_state("10.1.0.0/16") == "invalid"
    validator.withdraw("10.1.0.0/16")
    assert validator.rov_state("10.1.0.0/16") == "unknown"
    result = validator.install_route("10.1.0.0/16", 65001)
    assert result["installed"] is True
    assert result["state"] == "valid"


def test_failed_reload_keeps_previous_table_atomically():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    before = validator.snapshot()
    with pytest.raises(bgp_origin_validator.BgpError) as exc:
        validator.load_rois([{"prefix": "10.0.0.0/8", "origin_as": 65001}])
    assert exc.value.code == "BGP_ROV_LOAD_FAILED"
    assert validator.validate("10.1.0.0/16", 65001)["state"] == "valid"
    assert validator.snapshot() == before


def test_successful_reload_replaces_whole_table():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    first = validator.snapshot()["generation"]
    second = validator.load_rois([_roi(prefix="192.168.0.0/16", max_length=24)])
    assert second["generation"] == first + 1
    assert validator.validate("10.1.0.0/16", 65001)["state"] == "notfound"
    assert validator.validate("192.168.1.0/24", 65001)["state"] == "valid"


def test_newer_conflicting_roi_wins():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([
        _roi(max_length=24, updated_at=1),
        _roi(max_length=16, updated_at=2),
    ])
    assert validator.validate("10.1.2.0/24", 65001)["state"] == "invalid"
    assert validator.validate("10.1.0.0/16", 65001)["state"] == "valid"


def test_roi_family_is_not_applied_across_families():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi(prefix="2001:db8::/32", max_length=48)])
    assert validator.validate("10.1.0.0/16", 65001)["state"] == "notfound"
    validator.load_rois([_roi(prefix="10.0.0.0/8", max_length=24)])
    assert validator.validate("2001:db8:1::/48", 65001)["state"] == "notfound"


def test_two_validators_with_same_rois_agree():
    records = [_roi()]
    first = bgp_origin_validator.BgpOriginValidator()
    second = bgp_origin_validator.BgpOriginValidator()
    first.load_rois(records)
    second.load_rois(records)
    cases = [
        ("10.1.0.0/16", 65001, ()),
        ("10.1.0.0/16", 65002, ()),
        ("10.1.2.0/25", 65001, ()),
    ]
    for prefix, origin, path in cases:
        assert first.validate(prefix, origin, path) == second.validate(prefix, origin, path)


def test_soft_reconfigure_keeps_rov_stage():
    validator = bgp_origin_validator.BgpOriginValidator()
    result = validator.soft_reconfigure(["prefix-list", "rov", "community"])
    assert result["rov_enabled"] is True
    assert "rov" in result["stages"]
    assert result["stages"] == ["prefix-list", "rov", "community"]


def test_origin_set_must_not_be_treated_as_any_match():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    result = validator.validate("10.1.0.0/16", [65001, 65002])
    assert result["state"] == "invalid"
    assert result["code"] == "BGP_ROV_INVALID"


def test_advertise_lists_only_valid_installed_prefixes():
    validator = bgp_origin_validator.BgpOriginValidator()
    validator.load_rois([_roi()])
    validator.install_route("10.2.0.0/16", 65001)
    validator.install_route("10.1.0.0/16", 65001)
    validator.install_route("10.3.0.0/16", 65002)
    assert validator.advertise() == ["10.1.0.0/16", "10.2.0.0/16"]
