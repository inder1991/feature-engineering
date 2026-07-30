"""Windows to partition values — where look-ahead leakage lives.

`ExecutionInputs` needs the exact partitions to read; the plan carries windows. This is the half of
that translation that needs no cluster configuration, and the half where being wrong is invisible: a
window containing rows that had not landed yet makes a model look better in training than it can ever
be in production, and nothing in the output says so.

Two properties carry the weight, and each has a test that fails if it is dropped:

* the availability lag moves the CUTOFF, and therefore can move the PERIOD — 01:00 on 1 July with a
  48-hour lag is really asking as of 29 June, whose month is JUNE;
* a span of days is not a calendar period, so it is refused rather than widened.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.analysis.plan import Window
from featuregen.analysis.windows import (
    PartitionGranularity,
    WindowResolutionError,
    effective_cutoff,
    resolve_window,
    resolve_window_partitions,
)

_ANCHOR = "ftr::dpl_eib.tran_repos.tran_month"
_MONTH = PartitionGranularity.MONTH


def _w(label: str, *, offset: int = 0, length: int = 1, unit: str = "month", days: int = 30) -> Window:
    return Window(anchor_ref=_ANCHOR, length_days=days, offset_days=offset * days, label=label,
                  calendar_unit=unit, calendar_length=length, calendar_offset=offset)


# ── the pilot question's two periods ─────────────────────────────────────────────────────────────

def test_the_current_and_previous_months_resolve_to_the_pilot_partitions():
    ref = datetime(2026, 6, 30, 12, tzinfo=UTC)
    got = resolve_window_partitions((_w("current"), _w("previous", offset=1)),
                                    granularity=_MONTH, reference=ref)
    assert got == {"current": ("2026-06",), "previous": ("2026-05",)}


def test_a_multi_month_window_lists_every_month_most_recent_LAST():
    """A quarter is three partitions, ordered so a reader can see the span at a glance."""
    got = resolve_window(_w("quarter", length=3), granularity=_MONTH,
                         reference=datetime(2026, 6, 15, tzinfo=UTC))
    assert got == ("2026-04", "2026-05", "2026-06")


def test_a_window_crossing_a_year_boundary_counts_months_not_arithmetic_on_the_number():
    """January minus one month is December of the previous year — the case a naive `month - 1` gets
    wrong, and it produces a partition that does not exist rather than an error."""
    got = resolve_window(_w("previous", offset=1), granularity=_MONTH,
                         reference=datetime(2026, 1, 5, tzinfo=UTC))
    assert got == ("2025-12",)


def test_day_granularity_names_days():
    got = resolve_window(_w("yesterday", offset=1, unit="day"),
                         granularity=PartitionGranularity.DAY,
                         reference=datetime(2026, 6, 1, tzinfo=UTC))
    assert got == ("2026-05-31",)


# ── THE leakage property ─────────────────────────────────────────────────────────────────────────

def test_the_availability_lag_can_move_the_window_into_the_PREVIOUS_month():
    """The test this module exists for.

    Asked at 01:00 on 1 July against a table whose availability basis is `event_time_plus_lag` with a
    48-hour lag, the honest cutoff is 29 June — so "this month" is JUNE, not July. Resolving the
    period from the raw reference instant would read the July partition, which on 1 July holds one
    hour of data, and report it as a month. The comparison against "last month" would then show a
    catastrophic decrease for every customer in the bank.
    """
    ref = datetime(2026, 7, 1, 1, tzinfo=UTC)
    assert resolve_window(_w("current"), granularity=_MONTH, reference=ref) == ("2026-07",)
    assert resolve_window(_w("current"), granularity=_MONTH, reference=ref,
                          lag_hours=48) == ("2026-06",)


def test_the_lag_shifts_both_periods_together():
    """Shifting one period and not the other would compare June against April."""
    got = resolve_window_partitions((_w("current"), _w("previous", offset=1)), granularity=_MONTH,
                                    reference=datetime(2026, 7, 1, 1, tzinfo=UTC), lag_hours=48)
    assert got == {"current": ("2026-06",), "previous": ("2026-05",)}


def test_no_lag_leaves_the_cutoff_alone():
    ref = datetime(2026, 7, 1, 1, tzinfo=UTC)
    for lag in (None, 0):
        assert effective_cutoff(ref, lag_hours=lag) == ref


def test_a_negative_lag_is_refused_rather_than_moving_the_cutoff_forward():
    with pytest.raises(WindowResolutionError) as exc:
        effective_cutoff(datetime(2026, 7, 1, tzinfo=UTC), lag_hours=-24)
    assert exc.value.code == "AVAILABILITY_LAG_NEGATIVE"


def test_a_naive_reference_instant_is_refused():
    """A cutoff without a timezone is ambiguous by up to a day — enough to pick the wrong month on
    the first or last of it."""
    with pytest.raises(WindowResolutionError) as exc:
        effective_cutoff(datetime(2026, 7, 1, 1), lag_hours=48)  # noqa: DTZ001 — the point
    assert exc.value.code == "REFERENCE_INSTANT_NAIVE"


# ── a day span is not a calendar period ──────────────────────────────────────────────────────────

def test_a_DAY_SPAN_window_is_refused_against_monthly_partitions():
    """30 days ending 30 June overlaps 2026-05 and 2026-06. Reading both would fold late-May activity
    into "this month" — a wider answer presented as a narrower one, which is exactly the class of
    defect the whole planning layer exists to prevent."""
    with pytest.raises(WindowResolutionError) as exc:
        resolve_window(Window(anchor_ref=_ANCHOR, length_days=30, label="current"),
                       granularity=_MONTH, reference=datetime(2026, 6, 30, tzinfo=UTC))
    assert exc.value.code == "WINDOW_NOT_CALENDAR_ALIGNED"
    assert exc.value.subject == _ANCHOR


def test_a_window_in_the_wrong_UNIT_is_refused():
    with pytest.raises(WindowResolutionError) as exc:
        resolve_window(_w("current", unit="day"), granularity=_MONTH,
                       reference=datetime(2026, 6, 30, tzinfo=UTC))
    assert exc.value.code == "WINDOW_UNIT_MISMATCH"


@pytest.mark.parametrize("length,code", [(0, "WINDOW_EMPTY"), (-1, "WINDOW_EMPTY")])
def test_an_empty_window_is_refused(length, code):
    with pytest.raises(WindowResolutionError) as exc:
        resolve_window(_w("current", length=length), granularity=_MONTH,
                       reference=datetime(2026, 6, 30, tzinfo=UTC))
    assert exc.value.code == code


def test_a_negative_offset_reaching_past_the_cutoff_is_refused():
    with pytest.raises(WindowResolutionError) as exc:
        resolve_window(_w("next", offset=-1), granularity=_MONTH,
                       reference=datetime(2026, 6, 30, tzinfo=UTC))
    assert exc.value.code == "WINDOW_OFFSET_IN_THE_FUTURE"


# ── labels are identity ──────────────────────────────────────────────────────────────────────────

def test_an_unlabelled_window_is_refused():
    """`ExecutionInputs` is keyed by label. Falling back to position would swap two periods and
    invert a period-over-period answer — the same trap the bridge avoids by ordering on offset."""
    with pytest.raises(WindowResolutionError) as exc:
        resolve_window_partitions((_w(""),), granularity=_MONTH,
                                  reference=datetime(2026, 6, 30, tzinfo=UTC))
    assert exc.value.code == "WINDOW_UNLABELLED"


def test_two_windows_sharing_a_label_are_refused():
    with pytest.raises(WindowResolutionError) as exc:
        resolve_window_partitions((_w("current"), _w("current", offset=1)), granularity=_MONTH,
                                  reference=datetime(2026, 6, 30, tzinfo=UTC))
    assert exc.value.code == "WINDOW_LABEL_DUPLICATED"


# ── it feeds the bridge it was built for ─────────────────────────────────────────────────────────

def test_the_resolved_partitions_drive_the_bridge_end_to_end(db):
    """The point of all of it: a plan with calendar windows, resolved to partitions, translated to an
    IR, executed, and reconciled against the fixture's hand-counted answer."""
    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect
    from tests.featuregen.analysis.test_plan_to_execution import _grounded, _inputs, _plan
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    create_pilot_tables(db)
    windows = (_w("previous", offset=1), _w("current"))
    partitions = resolve_window_partitions(windows, granularity=_MONTH,
                                           reference=datetime(2026, 6, 30, tzinfo=UTC))
    ir = plan_to_execution_ir(_grounded(plan=_plan(windows=windows)),
                              _inputs(window_partitions=partitions))
    rows = run_analysis(db, ir, dialect=PostgresDialect())
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]
