# SPDX-License-Identifier: Apache-2.0
"""Due policies: pure functions of the run's day, computed in the instance time zone."""

from datetime import UTC, date, datetime

import pytest

from horae_github import due

FRIDAY = date(2026, 10, 30)


@pytest.mark.parametrize(
    ("start", "days", "expected"),
    [
        (FRIDAY, 0, FRIDAY),
        (FRIDAY, 1, date(2026, 11, 2)),
        (FRIDAY, 5, date(2026, 11, 6)),
        (date(2026, 10, 31), 1, date(2026, 11, 2)),  # Saturday
        (date(2026, 10, 28), 2, FRIDAY),
        (FRIDAY, None, None),
    ],
)
def test_business_days(start: date, days: int | None, expected: date | None) -> None:
    assert due.add_business_days(start, days) == expected


def test_calendar_days() -> None:
    assert due.add_days(FRIDAY, 2) == date(2026, 11, 1)
    assert due.add_days(FRIDAY, None) is None


def test_the_day_follows_the_instance_time_zone() -> None:
    late = datetime(2026, 10, 30, 21, 30, tzinfo=UTC)
    assert due.run_day(None, None, {"TZ": "Asia/Tokyo"}, lambda: late) == date(2026, 10, 31)
    assert due.run_day(None, None, {"TZ": "America/Los_Angeles"}, lambda: late) == FRIDAY
    assert due.run_day(None, "Asia/Tokyo", {"TZ": "UTC"}, lambda: late) == date(2026, 10, 31)
    assert due.run_day(None, None, {}, lambda: late) == FRIDAY
    assert due.run_day(None, None, {"TZ": ":/etc/localtime"}, lambda: late) == FRIDAY
    assert due.run_day("2026-01-02", "Asia/Tokyo", {}, lambda: late) == date(2026, 1, 2)


@pytest.mark.parametrize(
    ("today", "timezone", "message"),
    [
        ("30/10/2026", None, "YYYY-MM-DD"),
        ("2026-02-30", None, "real date"),
        ("2026-10-30\n", None, "YYYY-MM-DD"),
        ("\uff12\uff10\uff12\uff16-10-30", None, "YYYY-MM-DD"),  # fullwidth digits
        (
            "\u0662\u0660\u0662\u0666-\u0661\u0660-\u0663\u0660",
            None,
            "YYYY-MM-DD",
        ),  # Arabic-Indic digits
        ('"2026-10-30"', None, "YYYY-MM-DD"),
        (None, "Mars/Olympus", "unknown time zone"),
        (None, "../../etc/passwd", "unknown time zone"),
    ],
)
def test_bad_day_parameters(today: str | None, timezone: str | None, message: str) -> None:
    with pytest.raises(due.DueError, match=message):
        due.run_day(today, timezone, {}, due.utc_now)


def test_offsets_are_bounded() -> None:
    due.check_offset(None)
    due.check_offset(0)
    due.check_offset(365)
    for bad in (-1, 366):
        with pytest.raises(due.DueError):
            due.check_offset(bad)


@pytest.mark.parametrize(
    ("due_on", "expected"),
    [
        ("2026-11-06T07:00:00Z", date(2026, 11, 6)),
        ("2026-11-06T23:59:59Z", date(2026, 11, 6)),
        ("2026-02-30T07:00:00Z", None),
        ("2026-11-06T07:00:00.123+02:00", date(2026, 11, 6)),
        ("2026-11-06T07:00:00Z\n", None),
        ("2026-11-06Tjunk", None),
        ("\u0662\u0660\u0662\u0666-11-06T07:00:00Z", None),
        ("soon", None),
        (None, None),
        (20261106, None),
    ],
)
def test_milestone_day(due_on: object, expected: date | None) -> None:
    assert due.milestone_day(due_on) == expected
