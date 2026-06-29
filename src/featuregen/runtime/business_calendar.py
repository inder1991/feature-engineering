from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from featuregen.contracts import DbConn

_WALL_CLOCK_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(spec: str) -> tuple[int, str]:
    """Parse '7d' / '4h' / '30m' / '45s' into (amount, unit). 'd' = business days when a
    calendar is named, else calendar days; 'h'/'m'/'s' are always wall-clock."""
    spec = spec.strip().lower()
    if len(spec) < 2:
        raise ValueError(f"unparseable duration: {spec!r}")
    unit = spec[-1]
    if unit not in ("d", "h", "m", "s"):
        raise ValueError(f"unknown duration unit in {spec!r}")
    try:
        amount = int(spec[:-1])
    except ValueError as exc:
        raise ValueError(f"unparseable duration amount in {spec!r}") from exc
    if amount < 0:
        raise ValueError(f"negative duration not allowed: {spec!r}")
    return amount, unit


@dataclass(frozen=True, slots=True)
class _Calendar:
    workdays: frozenset[int]
    holidays: frozenset[date]


def _load_calendar(conn: DbConn, name: str) -> _Calendar:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT workdays, holidays FROM business_calendars WHERE calendar_name = %s",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown business calendar: {name!r}")
    workdays, holidays = row
    return _Calendar(frozenset(workdays), frozenset(holidays))


def _add_business_days(cal: _Calendar, start: datetime, days: int) -> datetime:
    cursor = start
    remaining = days
    while remaining > 0:
        cursor = cursor + timedelta(days=1)
        if cursor.isoweekday() in cal.workdays and cursor.date() not in cal.holidays:
            remaining -= 1
    return cursor


def resolve_business_deadline(
    conn: DbConn, calendar_name: str | None, start: datetime, spec: str
) -> datetime:
    """Resolve a duration spec to an absolute fire time (§5.5). Deterministic so timer
    deadlines reproduce on replay. 'd' against a named calendar counts BUSINESS days
    (skipping non-workdays + holidays); without a calendar 'd' is calendar days."""
    amount, unit = parse_duration(spec)
    if unit in _WALL_CLOCK_SECONDS:
        return start + timedelta(seconds=amount * _WALL_CLOCK_SECONDS[unit])
    if calendar_name is None:
        return start + timedelta(days=amount)
    return _add_business_days(_load_calendar(conn, calendar_name), start, amount)
