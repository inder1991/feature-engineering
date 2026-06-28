from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sp0.runtime.business_calendar import parse_duration, resolve_business_deadline

UTC = timezone.utc


def _seed_calendar(conn, name="ops", workdays=(1, 2, 3, 4, 5), holidays=()):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO business_calendars (calendar_name, timezone, workdays, holidays) "
            "VALUES (%s, 'UTC', %s, %s)",
            (name, list(workdays), list(holidays)),
        )


def test_parse_duration_units():
    assert parse_duration("7d") == (7, "d")
    assert parse_duration("4h") == (4, "h")
    assert parse_duration("30m") == (30, "m")
    assert parse_duration("45s") == (45, "s")


@pytest.mark.parametrize("bad", ["", "d", "7", "7x", "-3d", "abcd"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_wall_clock_units_ignore_calendar(conn):
    _seed_calendar(conn)
    start = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)  # a Friday
    assert resolve_business_deadline(conn, "ops", start, "4h") == start + timedelta(hours=4)
    assert resolve_business_deadline(conn, "ops", start, "90m") == start + timedelta(minutes=90)


def test_business_days_skip_weekend(conn):
    _seed_calendar(conn)
    friday = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)  # Fri 2026-06-26
    # 1 business day from Friday -> Monday 2026-06-29 (skip Sat/Sun)
    assert resolve_business_deadline(conn, "ops", friday, "1d").date().isoformat() == "2026-06-29"


def test_business_days_skip_holiday(conn):
    from datetime import date

    _seed_calendar(conn, holidays=(date(2026, 6, 29),))  # Monday is a holiday
    friday = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)
    # 1 business day -> skip Sat, Sun, Mon-holiday -> Tuesday 2026-06-30
    assert resolve_business_deadline(conn, "ops", friday, "1d").date().isoformat() == "2026-06-30"


def test_days_without_calendar_are_calendar_days(conn):
    start = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)
    assert resolve_business_deadline(conn, None, start, "2d") == start + timedelta(days=2)
