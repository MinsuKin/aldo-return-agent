from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from returns_domain import clock
from returns_domain.errors import DomainError

EDT = timezone(timedelta(hours=-4))


def test_demo_now_defaults_to_the_fixed_demo_instant():
    now = clock.demo_now()
    assert now == datetime(2026, 10, 16, 12, 0, tzinfo=EDT)
    assert now.utcoffset() == timedelta(hours=-4)
    assert clock.demo_today() == date(2026, 10, 16)
    assert clock.demo_today().strftime("%A") == "Friday"


def test_demo_now_does_not_move_with_the_real_clock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(clock, "now_utc", lambda: datetime(2031, 1, 1, tzinfo=UTC))
    assert clock.demo_today() == date(2026, 10, 16)


def test_demo_now_env_override_is_read_on_each_call(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEMO_NOW", "2026-12-01T09:00:00-05:00")
    assert clock.demo_today() == date(2026, 12, 1)
    # An instant given in UTC is converted to Toronto: 02:30Z on Oct 17 is 22:30 on Oct 16.
    monkeypatch.setenv("DEMO_NOW", "2026-10-17T02:30:00Z")
    assert clock.demo_today() == date(2026, 10, 16)


@pytest.mark.parametrize("bad", ["not a date", "2026-10-16T12:00:00", "2026-10-16"])
def test_demo_now_rejects_values_without_a_utc_offset(monkeypatch: pytest.MonkeyPatch, bad: str):
    monkeypatch.setenv("DEMO_NOW", bad)
    with pytest.raises(DomainError) as caught:
        clock.demo_now()
    assert caught.value.code == "INTERNAL_ERROR"
    assert "DEMO_NOW" in caught.value.message


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 9, 16), date(2026, 9, 16)),
        ("2026-09-16", date(2026, 9, 16)),
        (" 2026-09-16 ", date(2026, 9, 16)),
        (datetime(2026, 9, 16, 23, 59), date(2026, 9, 16)),  # naive: already Toronto local
        ("2026-09-16T23:59:00", date(2026, 9, 16)),
        # Aware values are converted to the Toronto calendar first.
        (datetime(2026, 10, 17, 2, 30, tzinfo=UTC), date(2026, 10, 16)),
        ("2026-10-17T02:30:00Z", date(2026, 10, 16)),
        ("2026-10-17T04:30:00Z", date(2026, 10, 17)),
        ("2026-10-17T04:30:00+00:00", date(2026, 10, 17)),
        (datetime(2026, 12, 1, 4, 30, tzinfo=UTC), date(2026, 11, 30)),  # winter, UTC-5
    ],
)
def test_to_toronto_date(value, expected: date):
    assert clock.to_toronto_date(value) == expected


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-13-45", "16/10/2026", None, 20261016, 1.5])
def test_to_toronto_date_rejects_garbage(bad: object):
    with pytest.raises(DomainError) as caught:
        clock.to_toronto_date(bad)  # type: ignore[arg-type]
    assert caught.value.code == "VALIDATION_ERROR"


def test_days_since_counts_toronto_calendar_days():
    assert clock.days_since("2026-10-04") == 12
    assert clock.days_since("2026-09-16") == 30
    assert clock.days_since("2026-09-15") == 31
    assert clock.days_since("2026-10-16") == 0
    assert clock.days_since("2026-09-16", now="2026-10-17T02:30:00Z") == 30
    assert clock.days_since("2026-09-16", now="2026-10-17T04:30:00Z") == 31


def test_iso_is_utc_with_whole_seconds():
    assert clock.iso(clock.demo_now()) == "2026-10-16T16:00:00+00:00"
    assert clock.iso(datetime(2026, 10, 16, 12, 0, 0, 987654, tzinfo=EDT)) == (
        "2026-10-16T16:00:00+00:00"
    )
    assert clock.iso(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00+00:00"


def test_iso_orders_lexicographically_like_time():
    earlier = clock.iso(datetime(2026, 10, 16, 9, 59, 59, tzinfo=UTC))
    later = clock.iso(datetime(2026, 10, 16, 10, 0, 0, tzinfo=UTC))
    assert earlier < later


def test_iso_requires_an_aware_datetime():
    with pytest.raises(DomainError) as caught:
        clock.iso(datetime(2026, 10, 16, 12, 0))
    assert caught.value.code == "VALIDATION_ERROR"


def test_parse_iso_round_trips_and_returns_utc():
    parsed = clock.parse_iso("2026-10-16T16:00:00+00:00")
    assert parsed == datetime(2026, 10, 16, 16, 0, tzinfo=UTC)
    assert clock.parse_iso("2026-10-16T12:00:00-04:00") == parsed
    assert clock.parse_iso(clock.iso(parsed)) == parsed
    assert clock.parse_iso("2026-10-16T16:00:00Z") == parsed


@pytest.mark.parametrize("bad", ["", "nope", "2026-10-16T16:00:00", None])
def test_parse_iso_rejects_naive_and_garbage(bad: object):
    with pytest.raises(DomainError):
        clock.parse_iso(bad)  # type: ignore[arg-type]


def test_now_utc_is_the_aware_real_clock_and_can_be_patched(monkeypatch: pytest.MonkeyPatch):
    real = clock.now_utc()
    assert real.tzinfo is not None
    assert real.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - real) < timedelta(seconds=5)
    frozen = datetime(2030, 5, 5, 5, 5, tzinfo=UTC)
    monkeypatch.setattr(clock, "now_utc", lambda: frozen)
    assert clock.now_utc() == frozen


def test_to_toronto_requires_an_aware_datetime():
    with pytest.raises(DomainError):
        clock.to_toronto(datetime(2026, 10, 16, 12, 0))


@pytest.mark.parametrize(
    ("day", "text"),
    [
        (date(2026, 10, 17), "Sat Oct 17"),
        (date(2026, 10, 16), "Fri Oct 16"),
        (date(2026, 10, 20), "Tue Oct 20"),
        (date(2026, 11, 2), "Mon Nov 2"),  # no zero padding
    ],
)
def test_format_day_derives_the_weekday_from_the_date(day: date, text: str):
    assert clock.format_day(day) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("2026-10-17", "Sat Oct 17"),  # contracts F.3: eta_text = clock.format_day(eta_date)
        (" 2026-10-20 ", "Tue Oct 20"),
        (datetime(2026, 10, 17, 2, 30, tzinfo=UTC), "Fri Oct 16"),  # Toronto calendar day
        ("2026-10-17T04:30:00Z", "Sat Oct 17"),
    ],
)
def test_format_day_accepts_iso_date_strings_and_aware_datetimes(value: object, text: str):
    assert clock.format_day(value) == text  # type: ignore[arg-type]


def test_format_day_rejects_garbage():
    with pytest.raises(DomainError):
        clock.format_day("next saturday")


# ----------------------------------------------------- hosts without a tz database (slim images)


def _toronto_zone() -> ZoneInfo:
    try:
        return ZoneInfo("America/Toronto")
    except ZoneInfoNotFoundError:  # pragma: no cover - only on hosts without tzdata
        pytest.skip("tz database not available on this host")


@pytest.mark.parametrize(
    ("utc_instant", "offset_hours"),
    [
        (datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC), -5),  # last second of EST
        (datetime(2026, 3, 8, 7, 0, 0, tzinfo=UTC), -4),  # DST starts: 02:00 EST -> 03:00 EDT
        (datetime(2026, 11, 1, 5, 59, 59, tzinfo=UTC), -4),  # last second of EDT
        (datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC), -5),  # DST ends: 02:00 EDT -> 01:00 EST
        (datetime(2026, 10, 16, 16, 0, 0, tzinfo=UTC), -4),
        (datetime(2027, 1, 15, 12, 0, 0, tzinfo=UTC), -5),
        (datetime(2027, 3, 14, 7, 0, 0, tzinfo=UTC), -4),
        (datetime(2027, 11, 7, 6, 0, 0, tzinfo=UTC), -5),
    ],
)
def test_fallback_offset_at_dst_boundaries(utc_instant: datetime, offset_hours: int):
    assert clock._fallback_offset(utc_instant) == timedelta(hours=offset_hours)
    assert _toronto_zone().utcoffset(utc_instant.replace(tzinfo=None).replace(tzinfo=UTC)) is not None


def test_fallback_matches_the_tz_database_across_two_years():
    zone = _toronto_zone()
    instant = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2028, 1, 1, tzinfo=UTC)
    while instant < end:
        expected = instant.astimezone(zone)
        actual = clock._to_toronto_fallback(instant)
        # Not `actual == expected`: PEP 495 makes aware datetimes in the repeated hour of a
        # DST change unequal to datetimes of other zones even for the same instant.
        assert actual.astimezone(UTC) == expected.astimezone(UTC) == instant
        assert actual.replace(tzinfo=None) == expected.replace(tzinfo=None)
        assert actual.utcoffset() == expected.utcoffset()
        assert actual.date() == expected.date()
        instant += timedelta(hours=1, minutes=7)  # walks through every hour of the day


def test_clock_still_works_when_the_tz_database_is_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(clock, "_zone", lambda: None)
    assert clock.demo_now() == datetime(2026, 10, 16, 12, 0, tzinfo=EDT)
    assert clock.demo_today() == date(2026, 10, 16)
    assert clock.to_toronto_date("2026-10-17T02:30:00Z") == date(2026, 10, 16)
    assert clock.to_toronto_date("2026-10-17T04:30:00Z") == date(2026, 10, 17)
    assert clock.days_since("2026-09-16", now="2026-10-17T02:30:00Z") == 30
