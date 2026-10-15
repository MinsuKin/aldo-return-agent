"""Clocks and America/Toronto date math.

Two clocks on purpose (handoff decision 3):

* ``demo_now()`` / ``demo_today()``: the fixed DEMO_NOW instant. ALL policy date math uses it.
* ``now_utc()``: the real wall clock. Token expiry, quote expiry and audit dates use it.
  Call it as ``clock.now_utc()`` (not ``from .clock import now_utc``) so tests can monkeypatch it.

Dates are ``date`` objects or ``YYYY-MM-DD`` strings on the America/Toronto calendar;
timestamps are ISO-8601 UTC strings from ``iso()`` (``2026-10-16T16:00:00+00:00``).
"""

from __future__ import annotations

import functools
import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .errors import DomainError, ErrorCode

TORONTO_TZ_NAME = "America/Toronto"

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ------------------------------------------------------------------ real wall clock (UTC)


def now_utc() -> datetime:
    """Real wall-clock time, timezone-aware UTC."""
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Aware datetime -> ``2026-10-16T16:00:00+00:00`` (UTC, whole seconds)."""
    if dt.utcoffset() is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "timestamps must be timezone-aware")
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(text: str) -> datetime:
    """ISO-8601 timestamp with an offset -> aware UTC datetime."""
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "not an ISO-8601 timestamp") from None
    if parsed.utcoffset() is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


# ------------------------------------------------------------------------ America/Toronto


@functools.lru_cache(maxsize=1)
def _zone() -> ZoneInfo | None:
    """The tz database entry, or None on hosts without tzdata (slim container images)."""
    try:
        return ZoneInfo(TORONTO_TZ_NAME)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7, weeks=n - 1)


def _fallback_offset(utc_dt: datetime) -> timedelta:
    """Toronto UTC offset by the post-2007 rule: DST from the second Sunday of March
    (02:00 EST = 07:00 UTC) to the first Sunday of November (02:00 EDT = 06:00 UTC)."""
    year = utc_dt.year
    start = datetime.combine(_nth_sunday(year, 3, 2), time(7, 0), tzinfo=UTC)
    end = datetime.combine(_nth_sunday(year, 11, 1), time(6, 0), tzinfo=UTC)
    return timedelta(hours=-4) if start <= utc_dt < end else timedelta(hours=-5)


def _to_toronto_fallback(dt: datetime) -> datetime:
    utc_dt = dt.astimezone(UTC)
    offset = _fallback_offset(utc_dt)
    name = "EDT" if offset == timedelta(hours=-4) else "EST"
    return utc_dt.astimezone(timezone(offset, name))


def to_toronto(dt: datetime) -> datetime:
    """Aware datetime -> the same instant in America/Toronto local time."""
    if dt.utcoffset() is None:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "expected a timezone-aware datetime")
    zone = _zone()
    return dt.astimezone(zone) if zone is not None else _to_toronto_fallback(dt)


def to_toronto_date(value: date | datetime | str) -> date:
    """Calendar date on the Toronto calendar.

    ``date`` and ``YYYY-MM-DD`` strings are taken as they are; aware datetimes (and ISO
    strings with an offset) are converted to Toronto first, so ``2026-10-17T02:30:00Z``
    is October 16; naive datetimes are assumed to be Toronto local already.
    """
    if isinstance(value, datetime):
        return value.date() if value.utcoffset() is None else to_toronto(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if _DATE_ONLY.match(text):
                return date.fromisoformat(text)
            return to_toronto_date(datetime.fromisoformat(text))
        except ValueError:
            pass
    raise DomainError(
        ErrorCode.VALIDATION_ERROR, "expected a date (YYYY-MM-DD) or an ISO-8601 timestamp"
    )


def demo_now() -> datetime:
    """The fixed DEMO_NOW instant (default 2026-10-16T12:00:00-04:00) in Toronto time."""
    raw = config.demo_now_setting()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR, "invalid configuration: DEMO_NOW must be ISO-8601"
        ) from None
    if parsed.utcoffset() is None:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR, "invalid configuration: DEMO_NOW must include a UTC offset"
        )
    return to_toronto(parsed)


def demo_today() -> date:
    """Today's Toronto calendar date under DEMO_NOW."""
    return demo_now().date()


def days_since(value: date | datetime | str, now: date | datetime | str | None = None) -> int:
    """Whole Toronto calendar days from ``value`` to ``now`` (default DEMO_NOW)."""
    end = demo_today() if now is None else to_toronto_date(now)
    return (end - to_toronto_date(value)).days


def format_day(value: date | datetime | str) -> str:
    """``date(2026, 10, 17)`` or ``"2026-10-17"`` -> ``"Sat Oct 17"``.

    The weekday always comes from the date itself (never hand-typed). Strings and aware
    datetimes are read on the Toronto calendar like everywhere else (``to_toronto_date``).
    """
    day = to_toronto_date(value)
    return f"{day.strftime('%a %b')} {day.day}"
