from __future__ import annotations

from datetime import timedelta


def parse_duration(s: str) -> timedelta:
    unit, n = s[-1], int(s[:-1])
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "m":
        return timedelta(minutes=n)
    raise ValueError(f"unsupported duration: {s!r}")
