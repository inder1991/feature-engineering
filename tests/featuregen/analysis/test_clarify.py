"""Abstentions become answerable questions, and answers become plans.

Intent extraction leaves a field empty and records why. Without this half that is useless: an honest
"the question does not say" and a broken extraction both arrive as an empty field, and the caller
cannot tell them apart.

Two properties carry it:

* the options come from the SAME bounded candidate set intent was given, so a clarification can never
  introduce a ref the plan was not grounded against;
* an answer is re-validated exactly as the model's output was — a clarification UI is the layer least
  able to guarantee that what came back is what it offered.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.clarify import ClarificationError, apply_answer, clarifications_for
from featuregen.analysis.intent import IntentCandidates, IntentExtraction
from featuregen.analysis.plan import AnalysisPlanV1, Measure, Window

_CIF = "ftr::tran_repos.cif_id"
_ACCT = "ftr::tran_repos.acct_id"
_MONTH = "ftr::tran_repos.tran_month"
_AMT = "ftr::tran_repos.tran_amt"
_SEG = "ftr::cust_dim.segment"


def _candidates() -> IntentCandidates:
    return IntentCandidates(
        column_refs=frozenset({_CIF, _ACCT, _MONTH, _AMT, _SEG}),
        table_refs=frozenset({"ftr::tran_repos", "ftr::cust_dim"}),
        labels={_CIF: "customer identifier", _MONTH: "posting period"},
        grain_refs=frozenset({_CIF, _ACCT}),
        as_of_refs=frozenset({_MONTH}))


def _plan(**over) -> AnalysisPlanV1:
    kw = dict(question="q", entity="customer", entity_ref="", base_table_ref="ftr::tran_repos",
              measure=Measure(op="count"),
              windows=(Window(anchor_ref=_MONTH, length_days=0, label="current",
                              calendar_unit="month", calendar_length=1, calendar_offset=0),))
    kw.update(over)
    return AnalysisPlanV1(**kw)


def _extraction(*unresolved: str) -> IntentExtraction:
    return IntentExtraction(plan=_plan(), unresolved=tuple(unresolved))


# ── the question offers the RIGHT few options ────────────────────────────────────────────────────

def test_the_entity_question_offers_identifiers_not_every_column():
    """Offering all sixty columns turns a decidable question into a search. The governed grain role
    is what narrows it — which is why retrieval now carries the role instead of discarding it."""
    (clar,) = clarifications_for(_extraction("entity"), _candidates())
    assert clar.code == "entity"
    assert {o.value for o in clar.options} == {_CIF, _ACCT}
    assert not clar.optional


def test_the_window_question_offers_TIME_columns():
    (clar,) = clarifications_for(_extraction("windows"), _candidates())
    assert {o.value for o in clar.options} == {_MONTH}


def test_the_options_carry_the_human_label_when_there_is_one():
    (clar,) = clarifications_for(_extraction("entity"), _candidates())
    assert {o.label for o in clar.options if o.value == _CIF} == {"customer identifier"}


def test_a_catalog_with_no_governed_grain_falls_back_to_every_column():
    """A narrower question is better; an EMPTY one is useless. With no grain fact the user is asked
    to pick from what there is, rather than being shown nothing."""
    plain = IntentCandidates(column_refs=frozenset({_CIF, _AMT}), table_refs=frozenset())
    (clar,) = clarifications_for(_extraction("entity"), plain)
    assert {o.value for o in clar.options} == {_CIF, _AMT}


# ── only what was raised, in an order that can be answered ───────────────────────────────────────

def test_nothing_is_asked_when_the_model_resolved_everything():
    """Asking about a confident answer invites a user to second-guess a good one."""
    assert clarifications_for(_extraction(), _candidates()) == ()


def test_the_entity_is_asked_before_the_dimensions():
    """The entity decides which table the rest of the question is about; answering dimensions first
    can mean answering twice."""
    clars = clarifications_for(_extraction("dimensions", "entity"), _candidates())
    assert [c.code for c in clars] == ["entity", "dimensions"]


def test_an_optional_abstention_is_marked_optional():
    """A plan with no dimensions is a good plan — one overall number. An entity is not optional."""
    by_code = {c.code: c for c in clarifications_for(
        _extraction("entity", "dimensions", "comparison"), _candidates())}
    assert by_code["entity"].optional is False
    assert by_code["dimensions"].optional is True
    assert by_code["dimensions"].allows_multiple is True
    assert by_code["comparison"].optional is True


# ── applying an answer ───────────────────────────────────────────────────────────────────────────

def test_answering_the_entity_fills_it_in():
    got = apply_answer(_plan(), "entity", (_CIF,), _candidates())
    assert got.entity_ref == _CIF


def test_answering_dimensions_accepts_several():
    got = apply_answer(_plan(), "dimensions", (_SEG, _AMT), _candidates())
    assert [d.logical_ref for d in got.dimensions] == [_SEG, _AMT]


def test_answering_the_window_reanchors_every_window_not_just_the_first():
    """Re-anchoring one period and not the other would compare two different clocks."""
    plan = _plan(windows=(
        Window(anchor_ref=_MONTH, length_days=0, label="current", calendar_unit="month",
               calendar_length=1, calendar_offset=0),
        Window(anchor_ref=_MONTH, length_days=0, label="previous", calendar_unit="month",
               calendar_length=1, calendar_offset=1)))
    got = apply_answer(plan, "windows", (_ACCT,), _candidates())
    assert {w.anchor_ref for w in got.windows} == {_ACCT}
    assert [w.calendar_offset for w in got.windows] == [0, 1]     # the periods themselves survive


def test_answering_the_comparison_uses_the_closed_vocabulary():
    assert apply_answer(_plan(), "comparison", ("increase",), _candidates()).comparison == "increase"
    assert apply_answer(_plan(), "comparison", ("",), _candidates()).comparison == ""


# ── an answer is validated like any other input ──────────────────────────────────────────────────

def test_an_answer_naming_a_column_that_was_never_offered_is_REFUSED():
    """THE property. Trusting the UI to have offered only legitimate options puts the guarantee in
    the layer least able to keep it — and a ref invented here grounds against nothing, exactly like a
    hallucinated one."""
    with pytest.raises(ClarificationError, match="was not offered"):
        apply_answer(_plan(), "entity", ("ftr::tran_repos.made_up",), _candidates())


def test_an_out_of_vocabulary_comparison_is_refused():
    with pytest.raises(ClarificationError, match="is not one of"):
        apply_answer(_plan(), "comparison", ("plummeted",), _candidates())


@pytest.mark.parametrize("chosen", [(), (_CIF, _ACCT)])
def test_the_entity_needs_exactly_one_column(chosen):
    with pytest.raises(ClarificationError, match="exactly one"):
        apply_answer(_plan(), "entity", chosen, _candidates())


def test_an_unknown_abstention_code_is_refused():
    with pytest.raises(ClarificationError, match="not an answerable abstention"):
        apply_answer(_plan(), "vibes", (_CIF,), _candidates())


def test_anchoring_windows_that_do_not_exist_says_so():
    """The period comes from the question, not from this answer — so there is nothing to re-anchor,
    and silently creating a window would invent the very thing the user abstained on."""
    with pytest.raises(ClarificationError, match="no windows to anchor"):
        apply_answer(_plan(windows=()), "windows", (_MONTH,), _candidates())


# ── the loop closes ──────────────────────────────────────────────────────────────────────────────

def test_an_answered_plan_reaches_execution(db):
    """End to end from an abstention: the model could not tell which column identified the customer,
    a human said, and the resulting plan produces the fixture's hand-counted answer."""
    from tests.featuregen.analysis.test_plan_to_execution import _inputs
    from tests.featuregen.analysis.test_plan_to_execution import _plan as pilot_plan
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.analysis.plan import GroundedPlan
    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect

    create_pilot_tables(db)
    blank = pilot_plan(entity_ref="")                    # the model abstained on the entity
    candidates = IntentCandidates(
        column_refs=frozenset({"ftr::dpl_eib.tran_repos.cif_id"}), table_refs=frozenset(),
        grain_refs=frozenset({"ftr::dpl_eib.tran_repos.cif_id"}))
    answered = apply_answer(blank, "entity", ("ftr::dpl_eib.tran_repos.cif_id",), candidates)
    assert answered.entity_ref == "ftr::dpl_eib.tran_repos.cif_id"

    # Partitions come from `_inputs`; window RESOLUTION is `test_window_partitions`' subject, and
    # this pilot plan carries day-span windows the resolver correctly refuses against month
    # partitions. What is under test here is that an ANSWERED plan executes.
    ir = plan_to_execution_ir(GroundedPlan(plan=answered, answerable=True), _inputs())
    rows = run_analysis(db, ir, dialect=PostgresDialect())
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]
