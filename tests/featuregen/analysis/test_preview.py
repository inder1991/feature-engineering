"""What will run, before it runs.

The property that makes a preview worth having is that it is assembled from the ARTIFACTS THAT
EXECUTE. A preview written independently of the compiler drifts from what runs, and a drifted preview
is worse than none: it converts "I checked" into false confidence.
"""
from __future__ import annotations

import pytest

from featuregen.analysis.plan import Finding, GroundedPlan
from featuregen.analysis.preview import preview
from featuregen.data_agent.sql_hive import HiveDialect
from featuregen.data_agent.sql_postgres import PostgresDialect
from tests.featuregen.analysis.test_plan_to_execution import _grounded, _inputs, _plan
from tests.featuregen.data_agent.pilot_fixture import CURRENT_MONTH, PREVIOUS_MONTH


# ── what will be computed ────────────────────────────────────────────────────────────────────────

def test_the_preview_describes_the_question_in_business_terms():
    got = preview(_grounded(), _inputs())
    assert got.entity == "customer"
    assert got.measure == "count of rows"
    assert got.comparison == "decrease"
    assert got.dimensions == ("ftr::dpl_eib.customer_segment_history.segment",
                              "ftr::dpl_eib.customer_segment_history.sector")


def test_the_partitions_that_will_be_READ_are_listed():
    """Partition pruning is part of the plan, not an optimisation. A user who sees two months can
    tell at a glance that this is not about to scan four years."""
    got = preview(_grounded(), _inputs())
    assert {p.label: p.partitions for p in got.periods} == {
        "previous": (PREVIOUS_MONTH,), "current": (CURRENT_MONTH,)}


# ── THE property: the SQL shown is the SQL that runs ─────────────────────────────────────────────

def test_the_sql_shown_is_byte_identical_to_what_the_executor_compiles():
    """Not "looks equivalent" — identical. The preview compiles the same IR through the same
    compiler, so drift between what a user approved and what ran is impossible by construction
    rather than by discipline."""
    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.data_agent.analysis import compile_analysis

    grounded, inputs = _grounded(), _inputs()
    ir = plan_to_execution_ir(grounded, inputs)
    assert preview(grounded, inputs).sql == compile_analysis(ir, dialect=PostgresDialect())


def test_the_preview_shows_the_dialect_that_will_EXECUTE():
    """Showing a user PostgreSQL and running HiveQL would defeat the point: the two disagree about
    things that change the answer rather than raising — a double-quoted token is an identifier in one
    and a string literal in the other."""
    hive = preview(_grounded(), _inputs(), dialect=HiveDialect()).sql
    assert "`cif_id`" in hive
    assert '"' not in hive


def test_the_plan_hash_identifies_the_computation_not_the_wording():
    """Two differently-worded questions that resolve to the same computation are the same plan — so a
    user who has approved one has, in substance, approved the other."""
    a = preview(_grounded(), _inputs())
    b = preview(_grounded(plan=_plan(question="a completely different sentence")), _inputs())
    assert a.plan_hash == b.plan_hash != ""


# ── what the answer rests on ─────────────────────────────────────────────────────────────────────

def test_findings_are_surfaced_with_their_subject_and_remedy():
    """This is where a finding finally reaches a human. `plan.py`'s rule is that findings travel with
    the answer rather than blocking it, which is only honest if something displays them."""
    grounded = _grounded(findings=(
        Finding(code="CURRENCY_MIXED", subject="ftr::dpl_eib.tran_repos.tran_amt",
                detail="two currencies present", clears_when="confirm a single currency"),))
    got = preview(grounded, _inputs())
    (finding,) = got.findings
    assert finding.code == "CURRENCY_MIXED"
    assert finding.subject == "ftr::dpl_eib.tran_repos.tran_amt"
    assert finding.clears_when == "confirm a single currency"
    assert got.rests_on_unconfirmed_facts


def test_a_clean_plan_says_so():
    got = preview(_grounded(), _inputs())
    assert got.findings == ()
    assert not got.rests_on_unconfirmed_facts
    assert got.runnable


def test_findings_do_NOT_make_a_plan_unrunnable():
    """The distinction the whole planning layer turns on: a doubt is disclosed, a refusal blocks."""
    grounded = _grounded(findings=(Finding(code="JOIN_IDENTITY_UNCONFIRMED", subject="x"),))
    assert preview(grounded, _inputs()).runnable


# ── a plan that cannot run says why ──────────────────────────────────────────────────────────────

def test_an_unanswerable_plan_reports_the_refusal_rather_than_a_confident_summary():
    got = preview(_grounded(answerable=False,
                            refusals=(("COLUMN_ABSENT", "ftr::dpl_eib.tran_repos.nope"),)),
                  _inputs())
    assert got.blocked_by == ("COLUMN_ABSENT", "ftr::dpl_eib.tran_repos.nope")
    assert got.sql == ""
    assert not got.runnable


def test_a_missing_eligibility_policy_is_named_as_the_thing_that_blocks_it():
    """The refusal IS the useful content — it names the decision a human still owes, rather than a
    generic "cannot run"."""
    got = preview(_grounded(), _inputs(eligibility=None))
    assert got.blocked_by is not None and got.blocked_by[0] == "ELIGIBILITY_ABSENT"
    assert got.sql == ""


def test_a_preview_with_no_execution_inputs_still_describes_the_question():
    """Useful before bindings exist: the user sees what would be computed and what is missing."""
    got = preview(_grounded())
    assert got.entity == "customer"
    assert got.blocked_by == ("EXECUTION_INPUTS_ABSENT", "ftr::dpl_eib.tran_repos")
    assert got.sql == ""


def test_a_blocked_preview_still_shows_its_findings():
    """A user fixing the blocker should be able to see, in the same view, what else the answer would
    have rested on — otherwise they fix one thing and meet the next in isolation."""
    grounded = _grounded(answerable=False, refusals=(("TABLE_ABSENT", "t"),),
                         findings=(Finding(code="GRAIN_NOT_ESTABLISHED", subject="t"),))
    got = preview(grounded, _inputs())
    assert [f.code for f in got.findings] == ["GRAIN_NOT_ESTABLISHED"]


# ── the preview matches the run ──────────────────────────────────────────────────────────────────

def test_running_the_previewed_sql_produces_the_previewed_shape(db):
    """The end of the chain: what the preview showed is what executes, and it returns the fixture's
    hand-counted answer."""
    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.data_agent.analysis import run_analysis
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    create_pilot_tables(db)
    grounded, inputs = _grounded(), _inputs()
    shown = preview(grounded, inputs)
    rows = run_analysis(db, plan_to_execution_ir(grounded, inputs), dialect=PostgresDialect())
    assert shown.runnable
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]
    assert len(rows) == EXPECTED["customer_rows"]


@pytest.mark.parametrize("op,expected", [("count", "count of rows")])
def test_a_bare_count_reads_as_rows_not_as_a_column(op, expected):
    assert preview(_grounded(), _inputs()).measure == expected
