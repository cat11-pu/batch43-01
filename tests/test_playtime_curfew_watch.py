import pytest

import playtime_curfew_watch as pcw

DAY = 86400


def make(**kwargs):
    return pcw.PlaytimeCurfewWatch(**kwargs)


def noon(day=0):
    return day * DAY + 12 * 3600


def test_cross_midnight_session_is_split_between_days():
    w = make()
    now = DAY + 1800
    w.record_session("acc", "phone", 3600, now=now)
    assert w.total_minutes("acc", 0) == pytest.approx(30.0)
    assert w.total_minutes("acc", 1) == pytest.approx(30.0)


def test_two_devices_with_same_interval_count_once():
    w = make()
    now = noon()
    w.record_session("acc", "phone", 1800, now=now)
    w.record_session("acc", "tablet", 1800, now=now)
    assert w.total_minutes("acc", 0) == pytest.approx(30.0)


def test_client_timezone_offset_does_not_bypass_curfew():
    w = make()
    late = 23 * 3600
    ahead = w.check("acc", late, client_tz_offset=5)
    behind = w.check("acc", late, client_tz_offset=-5)
    assert ahead["allowed"] is False
    assert behind["allowed"] is False
    assert ahead["reason"] == "CURFEW"


def test_parental_unbind_removes_limits():
    w = make()
    w.record_session("acc", "phone", 120 * 60, now=noon())
    assert w.check("acc", noon())["allowed"] is False
    w.set_parental_unbind("acc")
    assert w.check("acc", noon())["allowed"] is True


def test_afk_seconds_are_not_counted():
    w = make()
    out = w.record_session("acc", "phone", 3600, now=noon(),
                           afk_seconds=3000)
    assert out["counted_seconds"] == 600
    assert w.total_minutes("acc", 0) == pytest.approx(10.0)


def test_compensation_batch_is_idempotent():
    w = make()
    first = w.compensate("acc", 60, "batch-1", 0)
    second = w.compensate("acc", 60, "batch-1", 0)
    assert first["granted"] == 60
    assert second["granted"] == 0
    assert second["duplicate"] is True


def test_unavailable_cross_game_time_fails_closed():
    w = make()
    out = w.check("acc", noon(), foreign_minutes=None)
    assert out["allowed"] is False
    assert out["reason"] == "CROSS_GAME_UNAVAILABLE"


def test_adult_realname_lifts_curfew_immediately():
    w = make()
    w.set_birthdate("acc", 2007)
    out = w.check("acc", 23 * 3600, current_year=2026)
    assert out["allowed"] is True


def test_reminder_fires_only_once():
    w = make(daily_limit_minutes=90)
    w.record_session("acc", "phone", 89 * 60, now=noon())
    assert w.reminder_due("acc", noon()) is True
    assert w.reminder_due("acc", noon()) is False


def test_forced_logout_saves_progress_and_recovery_token():
    w = make()
    w.record_session("acc", "phone", 30 * 60, now=noon())
    out = w.force_offline("acc", noon())
    assert out["progress_saved"] is True
    assert out["recovery_token"]
    assert out["minutes"] == pytest.approx(30.0)


def test_holiday_uses_longer_limit():
    w = make(daily_limit_minutes=90, holiday_limit_minutes=180)
    w.record_session("acc", "phone", 100 * 60, now=noon())
    assert w.check("acc", noon())["allowed"] is False
    w.set_holiday(0, is_holiday=True)
    assert w.check("acc", noon())["allowed"] is True


def test_guest_minutes_merge_into_account():
    w = make()
    w.record_session("guest-1", "phone", 600, now=noon())
    out = w.link_guest("guest-1", "acc", noon())
    assert out["merged_minutes"] == pytest.approx(10.0)
    assert w.total_minutes("acc", 0) == pytest.approx(10.0)
    assert w.total_minutes("guest-1", 0) == 0


def test_totals_are_account_level_across_devices():
    w = make()
    w.record_session("acc", "phone", 600, now=noon())
    w.record_session("acc", "tablet", 600, now=noon() + 3600)
    total = w.total_minutes("acc", 0)
    assert total == pytest.approx(20.0)
    assert total >= 0


def test_curfew_denies_play_inside_window():
    w = make()
    assert w.check("acc", 23 * 3600)["allowed"] is False
    assert w.check("acc", 3 * 3600)["allowed"] is False
    assert w.check("acc", 12 * 3600)["allowed"] is True


def test_compensation_cap_holds_across_batches():
    w = make(compensation_cap_minutes=120)
    w.compensate("acc", 90, "batch-a", 0)
    second = w.compensate("acc", 90, "batch-b", 0)
    assert second["granted"] == 30
    third = w.compensate("acc", 90, "batch-c", 0)
    assert third["granted"] == 0
