"""Turn a plan's windows into the exact partition values the executor must read.

This is the half of ``ExecutionInputs`` that needs no cluster configuration, and the half where a
mistake is invisible. Two things make it load-bearing:

**A day span is not a calendar period.** A ``Window`` of 30 days ending 30 June overlaps the
``2026-05`` and ``2026-06`` partitions, so resolving it against a monthly-partitioned table by
"every partition the interval touches" folds late-May activity into "this month" — a wider answer
reported as a narrower one. Rather than guess, a day span against calendar partitions is REFUSED, and
``Window.calendar_unit`` exists so a plan can say what it means.

**The availability lag moves the cutoff, and therefore the period.** With basis
``event_time_plus_lag`` the data for an instant is not knowable until ``lag_hours`` later, so a
question asked at 01:00 on 1 July with a 48-hour lag is really asking as of 29 June — and its
"current month" is JUNE. Applying the lag after choosing the period, or not at all, produces a window
containing rows that had not landed yet: look-ahead leakage, which makes a model look better in
training than it can ever be in production. Here the lag is applied to the cutoff FIRST and the
period derived from the shifted cutoff.

The lag itself is not re-derived here. It comes from the governed ``AVAILABILITY_TIME`` fact via the
reader that already owns that vocabulary (``materialize/expression_ir``), and is passed in — so this
module stays a pure function over an instant, which is what makes the leakage case testable without
a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from featuregen.analysis.plan import Window


class WindowResolutionError(ValueError):
    """A window cannot be expressed as partition values. Carries the subject a human would fix."""

    def __init__(self, code: str, message: str, *, subject: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.subject = subject


class PartitionGranularity(StrEnum):
    """How the partition column names its periods. Closed: a granularity this module cannot format
    is refused rather than approximated, because an approximated partition label silently reads the
    wrong slice of the table."""

    MONTH = "month"     # e.g. 2026-06
    DAY = "day"         # e.g. 2026-06-15


def _label(moment: datetime, granularity: PartitionGranularity) -> str:
    if granularity is PartitionGranularity.MONTH:
        return f"{moment.year:04d}-{moment.month:02d}"
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"


def _shift_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + month - 1) - delta
    return index // 12, index % 12 + 1


def effective_cutoff(reference: datetime, *, lag_hours: float | None) -> datetime:
    """The instant the question can honestly be asked as of.

    Subtracting the lag BEFORE any period arithmetic is the whole point: it is what stops a window
    from including rows that had not arrived when the question was asked.
    """
    if reference.tzinfo is None:
        raise WindowResolutionError(
            "REFERENCE_INSTANT_NAIVE",
            "the reference instant carries no timezone; a cutoff without one is ambiguous by up to "
            "a day, which is enough to select the wrong partition")
    if not lag_hours:
        return reference
    if lag_hours < 0:
        raise WindowResolutionError(
            "AVAILABILITY_LAG_NEGATIVE",
            f"availability lag {lag_hours} is negative, which would move the cutoff FORWARD into "
            "data that cannot be known yet")
    return reference - timedelta(hours=float(lag_hours))


def resolve_window(window: Window, *, granularity: PartitionGranularity,
                   reference: datetime, lag_hours: float | None = None) -> tuple[str, ...]:
    """The partition values one window covers, most recent last.

    Refuses a day-span window against calendar partitions: see the module docstring for why widening
    is not a safe default.
    """
    cutoff = effective_cutoff(reference, lag_hours=lag_hours)

    if not window.calendar_unit:
        raise WindowResolutionError(
            "WINDOW_NOT_CALENDAR_ALIGNED",
            f"window {window.label!r} is a span of {window.length_days} days, and this partition "
            f"column names whole {granularity.value}s. The interval would overlap more partitions "
            "than the question asked for, so reading them would widen the answer without saying so "
            "— set `calendar_unit` to state the period the question means",
            subject=window.anchor_ref)

    if window.calendar_unit != granularity.value:
        raise WindowResolutionError(
            "WINDOW_UNIT_MISMATCH",
            f"window {window.label!r} is expressed in {window.calendar_unit!r} but the partition "
            f"column names {granularity.value!r} periods",
            subject=window.anchor_ref)

    length = int(window.calendar_length)
    if length < 1:
        raise WindowResolutionError(
            "WINDOW_EMPTY",
            f"window {window.label!r} spans {length} whole {window.calendar_unit}s; an empty window "
            "is either a mistake or an unbounded scan awaiting a fallback",
            subject=window.anchor_ref)

    offset = int(window.calendar_offset)
    if offset < 0:
        raise WindowResolutionError(
            "WINDOW_OFFSET_IN_THE_FUTURE",
            f"window {window.label!r} has offset {offset}: a negative offset reaches past the "
            "cutoff, into periods the question cannot know about",
            subject=window.anchor_ref)

    labels: list[str] = []
    # offset 0 is the period CONTAINING the cutoff; a length of L walks back L periods from there.
    for step in range(offset + length - 1, offset - 1, -1):
        if granularity is PartitionGranularity.MONTH:
            year, month = _shift_months(cutoff.year, cutoff.month, step)
            labels.append(_label(datetime(year, month, 1, tzinfo=UTC), granularity))
        else:
            labels.append(_label(cutoff - timedelta(days=step), granularity))
    return tuple(labels)


def resolve_window_partitions(windows: tuple[Window, ...], *,
                              granularity: PartitionGranularity, reference: datetime,
                              lag_hours: float | None = None) -> dict[str, tuple[str, ...]]:
    """Every window's partition values, keyed by the window's own label.

    The label is the key because that is what :class:`ExecutionInputs` is keyed by — position would
    silently swap two periods and invert a period-over-period answer.
    """
    resolved: dict[str, tuple[str, ...]] = {}
    for window in windows:
        if not window.label:
            raise WindowResolutionError(
                "WINDOW_UNLABELLED",
                "a window with no label cannot be matched to its partition values; position is not "
                "identity, and swapping two periods inverts a period-over-period answer",
                subject=window.anchor_ref)
        if window.label in resolved:
            raise WindowResolutionError(
                "WINDOW_LABEL_DUPLICATED",
                f"two windows share the label {window.label!r}", subject=window.anchor_ref)
        resolved[window.label] = resolve_window(
            window, granularity=granularity, reference=reference, lag_hours=lag_hours)
    return resolved
