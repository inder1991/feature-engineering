"""Grounding an analysis plan — the worked question, and each way it can be quietly wrong.

The worked question is the user's own: *"how many customers have fewer transactions this month,
split by segment and sector"*. It is modelled on the real FTR shape — a transaction table whose
grain is `tran_id`, carrying `cif_id` as a customer reference — because that is what the deployed
catalog actually holds.

Each test names ONE way a plausible-looking answer is wrong. They are written so that a grounding
implementation that simply returned "answerable, no findings" would fail every one of them.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.grounding import ground_analysis_plan
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure, Window
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

SRC = "ftr"
TBL = "tran_repos"


def _ref(column: str, *, source: str = SRC, table: str = TBL) -> str:
    return f"{source}::{table}.{column}"


def _catalog(db, *, rows=None, concepts=None):
    rows = rows or [
        CanonicalRow(SRC, TBL, "tran_id", "text", is_grain=True),
        CanonicalRow(SRC, TBL, "cif_id", "text"),
        CanonicalRow(SRC, TBL, "tran_amt", "numeric"),
        CanonicalRow(SRC, TBL, "tran_date", "timestamp", as_of=True),
    ]
    build_graph(db, SRC, rows, concepts=concepts or {})


def _govern(db, column, *, availability=False, grain=False):
    """Stand in for the governed fact links the confirm path writes."""
    sets, params = [], []
    if availability:
        sets.append("availability_fact_event_id = %s"); params.append("evt-avail")
    if grain:
        sets.append("grain_fact_event_id = %s"); params.append("evt-grain")
    db.execute(f"UPDATE graph_node SET {', '.join(sets)} WHERE catalog_source = %s AND column_name = %s",
               (*params, SRC, column))


def _plan(**over) -> AnalysisPlanV1:
    """The worked question, as a plan."""
    base = dict(
        question="how many customers have fewer transactions this month, by segment and sector",
        entity="customer", entity_ref=_ref("cif_id"), base_table_ref=f"{SRC}::{TBL}",
        measure=Measure(op="count", label="transactions"),
        windows=(Window(anchor_ref=_ref("tran_date"), length_days=30, offset_days=0, label="this month"),
                 Window(anchor_ref=_ref("tran_date"), length_days=30, offset_days=30, label="last month")),
        comparison="decrease",
    )
    base.update(over)
    return AnalysisPlanV1(**base)


@pytest.fixture
def catalog(db):
    _catalog(db)
    return db


# ── the worked question ──────────────────────────────────────────────────────────────────────────

def test_the_worked_question_is_answerable_and_says_what_it_rests_on(catalog):
    """The whole point: the question is ANSWERED, not refused, and every doubt is named. A young
    catalog produces findings — that is honest, not a failure."""
    grounded = ground_analysis_plan(catalog, _plan())
    assert grounded.answerable
    assert not grounded.trustworthy, "an ungoverned catalog must not look trustworthy"
    codes = {f.code for f in grounded.findings}
    assert "AVAILABILITY_BASIS_UNKNOWN" in codes   # the leak
    assert "GRAIN_NOT_ESTABLISHED" in codes        # counts rows, not customers


def test_a_fully_governed_catalog_produces_a_trustworthy_plan(catalog):
    """The other end: once the facts are confirmed, the same question carries nothing. This is what
    stops the findings being decorative — they have to be clearable."""
    _govern(catalog, "tran_date", availability=True)
    _govern(catalog, "cif_id", grain=True)
    grounded = ground_analysis_plan(catalog, _plan())
    assert grounded.trustworthy, [f"{f.code}:{f.detail}" for f in grounded.findings]


# ── each way the answer is quietly wrong ─────────────────────────────────────────────────────────

def test_an_unconfirmed_availability_basis_is_a_leak_finding(catalog):
    """THE most valuable check. FTR is a compliance table: if transactions post two days late, a
    "last 30 days" window measured on the event timestamp counts rows nobody could have seen. The
    catalog knows whether that basis was ever confirmed."""
    grounded = ground_analysis_plan(catalog, _plan())
    leak = [f for f in grounded.findings if f.code == "AVAILABILITY_BASIS_UNKNOWN"]
    assert leak and leak[0].subject == _ref("tran_date")
    assert leak[0].clears_when, "a leak finding must say what would clear it"


def test_a_declared_lag_must_actually_be_applied_to_the_cutoff(catalog):
    """Confirming the basis is not enough when the basis IS a lag: the cutoff has to move back by
    it, or the window is wrong in exactly the way the basis was recorded to prevent."""
    _govern(catalog, "tran_date", availability=True)
    plan = _plan(windows=(Window(anchor_ref=_ref("tran_date"), length_days=30,
                                 availability_basis="event_time_plus_lag"),))
    codes = {f.code for f in ground_analysis_plan(catalog, plan).findings}
    assert "AVAILABILITY_LAG_UNAPPLIED" in codes


def test_counting_per_customer_without_a_governed_grain_counts_rows(catalog):
    """"How many customers" over a table with no established grain silently answers "how many
    rows". The number looks completely reasonable."""
    codes = {f.code for f in ground_analysis_plan(catalog, _plan()).findings}
    assert "GRAIN_NOT_ESTABLISHED" in codes


def test_summing_amounts_across_currencies_is_flagged(db):
    """A transaction table with more than one currency column is telling you the amount's currency
    varies per row. Summing it adds dirhams to dollars."""
    _catalog(db, rows=[
        CanonicalRow(SRC, TBL, "tran_id", "text", is_grain=True),
        CanonicalRow(SRC, TBL, "cif_id", "text"),
        CanonicalRow(SRC, TBL, "tran_amt", "numeric", currency="AED"),
        CanonicalRow(SRC, TBL, "cp_amt", "numeric", currency="USD"),
        CanonicalRow(SRC, TBL, "tran_date", "timestamp", as_of=True),
    ])
    plan = _plan(measure=Measure(op="sum", logical_ref=_ref("tran_amt")))
    codes = {f.code for f in ground_analysis_plan(db, plan).findings}
    assert "CURRENCY_MIXED" in codes


def test_a_single_currency_table_is_not_flagged(db):
    """The control. Flagging every sum would make the finding noise, and noise gets ignored."""
    _catalog(db, rows=[
        CanonicalRow(SRC, TBL, "tran_id", "text", is_grain=True),
        CanonicalRow(SRC, TBL, "cif_id", "text"),
        CanonicalRow(SRC, TBL, "tran_amt", "numeric", currency="AED"),
        CanonicalRow(SRC, TBL, "tran_date", "timestamp", as_of=True),
    ])
    plan = _plan(measure=Measure(op="sum", logical_ref=_ref("tran_amt")))
    codes = {f.code for f in ground_analysis_plan(db, plan).findings}
    assert "CURRENCY_MIXED" not in codes


def test_summing_a_non_numeric_column_is_flagged(catalog):
    plan = _plan(measure=Measure(op="sum", logical_ref=_ref("cif_id")))
    codes = {f.code for f in ground_analysis_plan(catalog, plan).findings}
    assert "MEASURE_NOT_NUMERIC" in codes


# ── disclosure ───────────────────────────────────────────────────────────────────────────────────

def test_any_grouped_result_carries_the_small_cell_risk(db):
    """"By segment and sector" is the re-identification case: a sector with three customers names
    those three. This must be raised on the PLAN, before a chart exists to leak it."""
    _catalog(db, rows=[
        CanonicalRow(SRC, TBL, "tran_id", "text", is_grain=True),
        CanonicalRow(SRC, TBL, "cif_id", "text"),
        CanonicalRow(SRC, TBL, "segment", "text"),
        CanonicalRow(SRC, TBL, "tran_date", "timestamp", as_of=True),
    ], concepts={})
    plan = _plan(dimensions=(Dimension(logical_ref=_ref("segment"), label="segment"),))
    grounded = ground_analysis_plan(db, plan, min_cell_size=5)
    cell = [f for f in grounded.findings if f.code == "SMALL_CELL_RISK"]
    assert cell and "5" in cell[0].detail


def test_an_ungrouped_result_does_not_carry_it(catalog):
    codes = {f.code for f in ground_analysis_plan(catalog, _plan()).findings}
    assert "SMALL_CELL_RISK" not in codes


# ── cross-catalog ────────────────────────────────────────────────────────────────────────────────

def test_an_unconfirmed_identifier_link_is_carried_not_refused(catalog):
    """The user's directive: proposed links are USED, and the answer says what it stands on.
    Segment and sector live in another catalog, so this is the normal case, not the exception."""
    grounded = ground_analysis_plan(catalog, _plan(join_refs=("no-such-fact-key",)))
    assert grounded.answerable, "an unconfirmed link must not stop the answer"
    join = [f for f in grounded.findings if f.code == "JOIN_IDENTITY_UNCONFIRMED"]
    assert join and join[0].clears_when


# ── read scope, and absence ──────────────────────────────────────────────────────────────────────

def test_a_hidden_column_is_reported_as_absent_not_as_hidden(db):
    """A refusal that said "hidden" would confirm the column exists — an existence oracle for
    exactly the sensitive columns read scope protects. Same rule formula/tools already follows."""
    _catalog(db, rows=[
        CanonicalRow(SRC, TBL, "tran_id", "text", is_grain=True),
        CanonicalRow(SRC, TBL, "cif_id", "text"),
        CanonicalRow(SRC, TBL, "cust_name", "text", sensitivity="pii"),
        CanonicalRow(SRC, TBL, "tran_date", "timestamp", as_of=True),
    ])
    plan = _plan(dimensions=(Dimension(logical_ref=_ref("cust_name")),))
    blind = ground_analysis_plan(db, plan, roles=())
    assert not blind.answerable
    assert ("COLUMN_ABSENT", _ref("cust_name")) in blind.refusals
    assert all(code != "COLUMN_NOT_VISIBLE" for code, _ in blind.refusals)
    assert ground_analysis_plan(db, plan, roles=("pii_reader",)).answerable


def test_a_column_the_catalog_does_not_describe_refuses(catalog):
    plan = _plan(measure=Measure(op="sum", logical_ref=_ref("no_such_column")))
    grounded = ground_analysis_plan(catalog, plan)
    assert not grounded.answerable
    assert ("COLUMN_ABSENT", _ref("no_such_column")) in grounded.refusals


# ── period-over-period coherence ─────────────────────────────────────────────────────────────────

def test_comparing_two_windows_on_different_anchors_is_refused(catalog):
    """"Fewer than last month" measured on two different date columns compares two differently
    shaped months — the trend would be an artefact of the metadata, not the data."""
    plan = _plan(windows=(Window(anchor_ref=_ref("tran_date"), length_days=30),
                          Window(anchor_ref=_ref("tran_id"), length_days=30, offset_days=30)))
    assert not ground_analysis_plan(catalog, plan).answerable
