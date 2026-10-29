# SPDX-License-Identifier: Apache-2.0
"""Due dates: a pure function of the run's day, so the same day gives the same tasks.

The day is the `today` parameter (pass `"{{date}}"` from the pipeline: the run's date in the
instance time zone), else the clock read in `timezone` (the parameter, else the `TZ` the
plugin host sets to the instance time zone, else UTC).
"""

import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
DATETIME_RE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})T")
MAX_OFFSET_DAYS = 365


class DueError(ValueError):
    """A bad `today`, `timezone` or offset parameter (fixed message)."""


def zone(name: str | None, environ: Mapping[str, str]) -> ZoneInfo:
    """`name` (refused when unknown), else the host's `TZ` (ignored when unknown), else UTC."""
    if name is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise DueError("github: unknown time zone") from None
    try:
        return ZoneInfo(environ.get("TZ") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def run_day(
    today: str | None,
    timezone: str | None,
    environ: Mapping[str, str],
    clock: Callable[[], datetime],
) -> date:
    if today is not None:
        if not DATE_RE.match(today):
            raise DueError("github: today must be YYYY-MM-DD")
        try:
            return date.fromisoformat(today)
        except ValueError:
            raise DueError("github: today must be a real date") from None
    return clock().astimezone(zone(timezone, environ)).date()


def check_offset(days: int | None) -> None:
    if days is not None and not 0 <= days <= MAX_OFFSET_DAYS:
        raise DueError(f"github: due offsets are 0 to {MAX_OFFSET_DAYS} days")


def add_days(day: date, days: int | None) -> date | None:
    return None if days is None else day + timedelta(days=days)


def add_business_days(day: date, days: int | None) -> date | None:
    """`days` working days (Monday to Friday) after `day`; 0 is `day` itself. No holidays."""
    if days is None:
        return None
    while days > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days -= 1
    return day


def milestone_day(due_on: object) -> date | None:
    """A milestone's due date. GitHub stores a date and reports it as a UTC timestamp
    (`2026-11-06T07:00:00Z` for 6 November), so the UTC calendar date is the one set; reading
    it in the instance time zone could move it a day earlier west of UTC."""
    if not isinstance(due_on, str):
        return None
    match = DATETIME_RE.match(due_on)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)
