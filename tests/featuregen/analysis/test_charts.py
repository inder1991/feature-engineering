"""A chart is a disclosure surface.

The result is one row per customer — individual-level data — and drawing it is publishing it. A
grouped chart is worse than a table for this, because a bar of height one reads instantly as "there
is exactly one person in this segment, and here is their activity".

So the tests here are mostly about what must NOT appear on the screen.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.charts import ChartKind, choose_chart
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure
from featuregen.data_agent.analysis import AnalysisRow


def _plan(**over) -> AnalysisPlanV1:
    kw = dict(question="q", entity="customer", entity_ref="ftr::t.cif_id",
              base_table_ref="ftr::t", measure=Measure(op="count"), comparison="decrease",
              dimensions=(Dimension(logical_ref="ftr::cust_dim.segment"),))
    kw.update(over)
    return AnalysisPlanV1(**kw)


def _rows(segments: dict[str, int], *, declining: bool = True) -> tuple[AnalysisRow, ...]:
    """`{segment: how many customers}`, each declining unless told otherwise."""
    out = []
    n = 0
    for segment, count in segments.items():
        for _ in range(count):
            n += 1
            out.append(AnalysisRow(key=f"C{n}", previous_count=3 if declining else 1,
                                   current_count=1 if declining else 3,
                                   dimensions={"segment": segment}))
    return tuple(out)


# ── THE property: a chart never plots a person ───────────────────────────────────────────────────

def test_no_point_is_ever_an_ENTITY():
    """Every point is a count of customers in a group. A per-customer chart of the pilot result would
    put six customers and their transaction counts on one screen."""
    rows = _rows({"SME": 6, "CORPORATE": 7})
    spec = choose_chart(rows, _plan())
    assert {p.label for p in spec.points} == {"SME", "CORPORATE"}
    assert not any(p.label.startswith("C") and p.label[1:].isdigit() for p in spec.points)
    assert sum(p.value for p in spec.points) == 13


def test_a_group_smaller_than_the_threshold_is_SUPPRESSED():
    """A group of one IS that person."""
    spec = choose_chart(_rows({"SME": 6, "VIP": 1}), _plan(), min_cell_size=5)
    assert {p.label for p in spec.points} == {"SME"}
    assert spec.suppressed_groups == 1
    assert spec.suppressed_entities == 1


def test_suppression_is_REPORTED_not_silent():
    """A chart that quietly drops three segments looks like a complete picture of the population —
    a worse lie than showing nothing."""
    spec = choose_chart(_rows({"SME": 9, "A": 2, "B": 1, "C": 3}), _plan(), min_cell_size=5)
    assert spec.is_partial
    assert spec.suppressed_groups == 3
    assert spec.suppressed_entities == 6
    assert "SMALL_CELL_RISK" in spec.findings


def test_nothing_is_drawn_when_EVERY_group_is_too_small():
    """Falling back to "show it anyway" is exactly the pressure this must resist."""
    spec = choose_chart(_rows({"A": 2, "B": 1}), _plan(), min_cell_size=5)
    assert spec.kind is ChartKind.NONE
    assert spec.points == ()
    assert "identify the individuals" in spec.reason
    assert "SMALL_CELL_RISK" in spec.findings


def test_a_threshold_of_zero_cannot_be_asked_for():
    """A zero threshold suppresses nothing and turns this into a plotting helper — the one thing it
    must not silently become."""
    spec = choose_chart(_rows({"VIP": 1}), _plan(), min_cell_size=0)
    assert spec.min_cell_size == 1
    assert {p.label for p in spec.points} == {"VIP"}    # honest at 1, but never at 0


def test_a_clean_result_reports_no_suppression():
    spec = choose_chart(_rows({"SME": 9, "CORPORATE": 8}), _plan(), min_cell_size=5)
    assert not spec.is_partial
    assert spec.findings == ()


# ── the kind is chosen from the shape ────────────────────────────────────────────────────────────

def test_a_grouped_comparison_is_a_BAR():
    assert choose_chart(_rows({"SME": 9}), _plan()).kind is ChartKind.BAR


def test_an_UNGROUPED_comparison_is_a_summary_of_two_counts():
    """Still aggregates: how many met the comparison and how many did not, never who."""
    rows = _rows({"SME": 4}) + _rows({"SME": 3}, declining=False)
    spec = choose_chart(rows, _plan(dimensions=()))
    assert spec.kind is ChartKind.SUMMARY
    assert {(p.label, p.value) for p in spec.points} == {("decrease", 4), ("no decrease", 3)}


def test_no_comparison_and_no_grouping_has_nothing_safe_to_draw():
    """The only thing left to plot would be the rows themselves."""
    spec = choose_chart(_rows({"SME": 9}), _plan(dimensions=(), comparison=""))
    assert spec.kind is ChartKind.NONE
    assert "the rows themselves" in spec.reason


def test_an_empty_result_draws_nothing_rather_than_an_empty_axis():
    spec = choose_chart((), _plan())
    assert spec.kind is ChartKind.NONE
    assert spec.reason


def test_only_the_FIRST_dimension_is_charted():
    """A chart with two group-bys is a table, and pretending otherwise produces a picture nobody can
    read."""
    plan = _plan(dimensions=(Dimension(logical_ref="ftr::cust_dim.segment"),
                             Dimension(logical_ref="ftr::cust_dim.sector")))
    spec = choose_chart(_rows({"SME": 9}), plan)
    assert spec.x_label == "segment"


# ── ordering and labelling ───────────────────────────────────────────────────────────────────────

def test_points_are_ordered_by_size_then_name_so_the_chart_is_stable():
    """A chart whose bars reorder between two runs of the same question reads as changed data."""
    spec = choose_chart(_rows({"B": 6, "A": 6, "C": 9}), _plan(), min_cell_size=5)
    assert [p.label for p in spec.points] == ["C", "A", "B"]


def test_a_missing_dimension_value_is_a_named_bucket_not_a_dropped_row():
    """Dropping unclassified customers would make the totals stop reconciling with the answer."""
    rows = tuple(AnalysisRow(key=f"C{i}", previous_count=3, current_count=1, dimensions={})
                 for i in range(6))
    spec = choose_chart(rows, _plan(), min_cell_size=5)
    assert [(p.label, p.value) for p in spec.points] == [("Unknown", 6)]


# ── it reconciles with the answer it came from ───────────────────────────────────────────────────

def test_the_chart_totals_reconcile_with_the_hand_counted_fixture(db):
    """Built from a real run: the bars must add up to the customers the answer says declined, or the
    picture and the number disagree."""
    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect
    from tests.featuregen.analysis.test_plan_to_execution import _grounded, _inputs
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    create_pilot_tables(db)
    grounded = _grounded()
    rows = run_analysis(db, plan_to_execution_ir(grounded, _inputs()), dialect=PostgresDialect())

    # The pilot has two decliners in two different segments — every group is below any sane
    # threshold, so the honest chart is NOTHING. That is the correct outcome on a six-customer
    # fixture, and the reason a demo dataset must not set the policy.
    spec = choose_chart(rows, grounded.plan, min_cell_size=5)
    assert spec.kind is ChartKind.NONE
    assert spec.suppressed_entities == len(EXPECTED["decreased_customers"])

    # At a threshold of one the bars reconcile with the answer.
    spec = choose_chart(rows, grounded.plan, min_cell_size=1)
    assert sum(p.value for p in spec.points) == len(EXPECTED["decreased_customers"])
    assert {p.label for p in spec.points} == set(EXPECTED["decreased_by_segment"])
