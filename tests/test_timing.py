from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import timing
from app.timing import compute_call_time, is_call_due

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 23, 10, 0, tzinfo=IST)
H = timedelta(hours=1)


def plan_for(remaining: timedelta):
    return compute_call_time(NOW + remaining, NOW)


# ---------- remaining >= 12 hours ----------

@pytest.mark.parametrize("remaining", [12 * H, 13 * H, 3 * 24 * H])
def test_12_hours_or_more_calls_12h_before(remaining):
    plan = plan_for(remaining)
    assert plan.reason == timing.BEFORE_12H
    assert plan.call_at == NOW + remaining - 12 * H


def test_exactly_12_hours_calls_now():
    plan = plan_for(12 * H)
    assert plan.call_at == NOW


# ---------- 2h <= remaining < 12h ----------

@pytest.mark.parametrize("remaining", [2 * H, 5 * H, 12 * H - timedelta(seconds=1)])
def test_2_to_12_hours_calls_2h_before(remaining):
    plan = plan_for(remaining)
    assert plan.reason == timing.BEFORE_2H
    assert plan.call_at == NOW + remaining - 2 * H


# ---------- 1h <= remaining < 2h ----------

@pytest.mark.parametrize("remaining", [1 * H, 90 * timedelta(minutes=1), 2 * H - timedelta(seconds=1)])
def test_1_to_2_hours_calls_1h_before(remaining):
    plan = plan_for(remaining)
    assert plan.reason == timing.BEFORE_1H
    assert plan.call_at == NOW + remaining - 1 * H


# ---------- 0 < remaining < 1h ----------

@pytest.mark.parametrize("remaining", [timedelta(seconds=1), timedelta(minutes=30), H - timedelta(seconds=1)])
def test_under_1_hour_calls_immediately(remaining):
    plan = plan_for(remaining)
    assert plan.reason == timing.IMMEDIATE
    assert plan.call_at == NOW


# ---------- deadline passed ----------

@pytest.mark.parametrize("remaining", [timedelta(0), -timedelta(seconds=1), -5 * H])
def test_deadline_passed_means_no_call(remaining):
    plan = plan_for(remaining)
    assert plan.reason == timing.PASSED
    assert plan.call_at is None
    assert plan.should_call is False


# ---------- no deadline ----------

def test_no_deadline_uses_default_60_minutes():
    plan = compute_call_time(None, NOW)
    assert plan.reason == timing.NO_DEADLINE
    assert plan.call_at == NOW + timedelta(minutes=60)


def test_no_deadline_custom_minutes():
    assert compute_call_time(None, NOW, no_deadline_call_minutes=15).call_at == NOW + timedelta(minutes=15)


# ---------- timezones ----------

def test_works_across_timezones():
    deadline_utc = datetime(2026, 11, 23, 18, 30, tzinfo=timezone.utc)  # = 24 Nov 00:00 IST
    plan = compute_call_time(deadline_utc, NOW)  # 14 h left
    assert plan.reason == timing.BEFORE_12H
    assert plan.call_at == datetime(2026, 11, 23, 12, 0, tzinfo=IST)


def test_naive_times_are_rejected():
    with pytest.raises(ValueError):
        compute_call_time(datetime(2026, 11, 24), NOW)
    with pytest.raises(ValueError):
        compute_call_time(None, datetime(2026, 11, 24))


def test_call_time_is_never_before_processing():
    # For every case the call must not be scheduled in the past
    for minutes in range(1, 60 * 30, 7):
        plan = plan_for(timedelta(minutes=minutes))
        assert plan.call_at >= NOW
        assert plan.call_at < NOW + timedelta(minutes=minutes)  # and always before the deadline


def test_is_call_due():
    assert is_call_due(NOW, NOW) is True
    assert is_call_due(NOW + H, NOW) is False
    assert is_call_due(NOW - H, NOW) is True
