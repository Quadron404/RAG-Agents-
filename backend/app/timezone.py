"""Timezone handling: validation, persistence, and local calendar-day boundaries.

Timezones are IANA names (``America/New_York``, ``Europe/London`` ...).  The
owner's configured timezone lives on the ``users`` row and defaults to ``UTC``,
which is the documented fallback whenever a value is missing or unrecognised.

Calendar-day boundaries are computed in the owner's local time and expressed as
UTC epoch seconds, so a daily summary can be scoped to "all of 2026-10-24 in
America/New_York" without guessing at a fixed 24-hour offset -- that offset is
23 hours on a spring-forward day and 25 hours on a fall-back day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Documented fallback: an empty or unrecognised timezone resolves to UTC.
UTC_FALLBACK = "UTC"


def validate_timezone(value: object) -> str:
    """Return ``value`` when it is a valid IANA timezone name, else ``UTC``.

    Never raises: an unknown or empty timezone degrades to UTC so a malformed
    client preference can never break a request or leak across accounts.
    """
    if value is None:
        return UTC_FALLBACK
    name = str(value).strip()
    if not name:
        return UTC_FALLBACK
    try:
        ZoneInfo(name)  # ZoneInfoNotFoundError when the name is unknown
    except ZoneInfoNotFoundError:
        return UTC_FALLBACK
    return name


def _zone(tz_name: str):
    """A :class:`zoneinfo.ZoneInfo` for ``tz_name``, falling back to UTC."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return dt_timezone.utc


def local_date_key(tz_name: str, when: float) -> str:
    """The local calendar date (``YYYY-MM-DD``) for ``when`` in ``tz_name``."""
    zone = _zone(tz_name)
    return datetime.fromtimestamp(when, tz=zone).date().isoformat()


def local_day_boundaries(tz_name: str, when: float) -> tuple[float, float]:
    """``(start, end)`` UTC epoch seconds for the local day containing ``when``.

    ``start`` is 00:00:00 local and ``end`` is 00:00:00 the next local day.  The
    next day is found by advancing one calendar date in local time, so a
    spring-forward day spans 23 hours and a fall-back day 25 hours -- no message
    near the transition is silently skipped or double-counted.
    """
    zone = _zone(tz_name)
    local = datetime.fromtimestamp(when, tz=zone)
    start_local = datetime(local.year, local.month, local.day, tzinfo=zone)
    next_local = start_local + timedelta(days=1)
    end_local = datetime(next_local.year, next_local.month, next_local.day, tzinfo=zone)
    return start_local.timestamp(), end_local.timestamp()
