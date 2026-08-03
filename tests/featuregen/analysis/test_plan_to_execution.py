"""The seam between the two halves of the data agent.

`featuregen.analysis` turns a question into a grounded `AnalysisPlanV1` — catalog identities, typed
findings, no SQL. `featuregen.data_agent` turns an `AnalysisExecutionIRV1` into SQL and runs it,
proven against a real HiveServer2. Nothing connected them: `ground_analysis_plan` had no caller in
`src/`, and nothing outside `data_agent/analysis.py` mentioned the execution IR. Two islands.

That gap is why five gap codes — `POPULATION_UNRESOLVED`, `PHYSICAL_BINDING_MISSING`,
`POINT_IN_TIME_RULE_MISSING`, `SEMANTIC_TERM_UNRESOLVED`, `DIMENSION_UNRESOLVED` — existed with zero
references anywhere outside the vocabulary that declares them. They are what this translation
discovers, which is fair evidence it was designed and then not built.

**The bridge's job is mostly to refuse precisely.** Three things the executor requires cannot be
expressed in `AnalysisPlanV1` at all, and the honest response is a typed refusal naming each, not a
default invented here:

* a POPULATION SPINE distinct from the event table. The plan carries one `base_table_ref`; the IR's
  whole correctness property is that the spine is the left side and the events hang off it, so a
  customer who fell to zero survives. Collapsing them silently would delete that property.
* an ELIGIBILITY policy. The plan cannot say which rows count, and the IR refuses without one
  because pending, failed and reversed transactions would read as activity.
* PARTITION VALUES. A `Window` is a length in days; the executor needs the exact partition values to
  read. Converting one to the other needs the partition column's calendar and, where availability
  basis is `event_time_plus_lag`, a cutoff shifted back by that lag — otherwise the window includes
  rows that had not landed yet.

Every refusal maps to a learning gap, so a question blocked here lands in the queue a human works
rather than vanishing into a stack trace.
"""
from __future__ import annotations

import pytest
from tests.featuregen.data_agent.pilot_fixture import (
    CURRENT_MONTH,
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    DIMENSION_TABLE,
    PILOT_JOIN_EVIDENCE,
    PREVIOUS_MONTH,
    TRANSACTION_SCHEMA,
    TRANSACTION_TABLE,
    binding,
)
from tests.featuregen.data_agent.test_analysis_ir import _attribution, _policy
from tests.featuregen.materialize.test_cross_catalog_ir import (
    _inventory,
    _realization_current,
)
from tests.featuregen.materialize.test_expression_ir import INVENTORY

from featuregen.analysis.execution import (
    BRIDGE_REFUSAL_TO_GAP,
    BridgeRefusal,
    ExecutionInputs,
    plan_to_execution_ir,
)
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, GroundedPlan, Measure, Window
from featuregen.data_agent.analysis import Comparison
from featuregen.data_agent.learning import GAP_CODES
from featuregen.overlay.upload.source_selection import SELECTION_POPULATION_UNDECLARED


def _plan(**over) -> AnalysisPlanV1:
    kw = dict(
        question="customers whose transaction count decreased this month, by segment and sector",
        entity="customer", entity_ref="ftr::dpl_eib.tran_repos.cif_id",
        base_table_ref="ftr::dpl_eib.tran_repos",
        measure=Measure(op="count", label="transactions"),
        windows=(Window(anchor_ref="ftr::dpl_eib.tran_repos.tran_month", length_days=30,
                        offset_days=30, label="previous"),
                 Window(anchor_ref="ftr::dpl_eib.tran_repos.tran_month", length_days=30,
                        offset_days=0, label="current")),
        dimensions=(Dimension(logical_ref="ftr::dpl_eib.customer_segment_history.segment"),
                    Dimension(logical_ref="ftr::dpl_eib.customer_segment_history.sector")),
        comparison="decrease",
    )
    kw.update(over)
    return AnalysisPlanV1(**kw)


def _grounded(**over) -> GroundedPlan:
    kw = dict(plan=_plan(), answerable=True)
    kw.update(over)
    return GroundedPlan(**kw)


def _inputs(**over) -> ExecutionInputs:
    """Everything the plan contract cannot carry, supplied explicitly. The bridge names each as its
    own refusal when absent rather than inventing it."""
    kw = dict(
        spine_binding=binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE), spine_key_column="cif_id",
        event_binding=binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE), event_key_column="cif_id",
        period_column="tran_month",
        window_partitions={"previous": (PREVIOUS_MONTH,), "current": (CURRENT_MONTH,)},
        eligibility=_policy(),
        dimension_binding=binding(CUSTOMER_SCHEMA, DIMENSION_TABLE),
        attribution=_attribution(),
        join_evidence=PILOT_JOIN_EVIDENCE,
    )
    kw.update(over)
    return ExecutionInputs(**kw)


# ── the happy path produces an IR that actually runs ─────────────────────────────────────────────

def test_a_grounded_plan_becomes_an_executable_ir():
    ir = plan_to_execution_ir(_grounded(), _inputs())
    assert ir.question == _plan().question
    assert ir.comparison is Comparison.DECREASED
    assert ir.period_column == "tran_month"
    assert ir.current.values == (CURRENT_MONTH,)
    assert ir.previous.values == (PREVIOUS_MONTH,)


def test_the_dimensions_arrive_as_COLUMN_names_not_logical_refs():
    """The plan speaks catalog identities; SQL needs the bare column. Passing a logical_ref through
    would be refused by `require_identifier` — the `::` and dots are not an identifier."""
    ir = plan_to_execution_ir(_grounded(), _inputs())
    assert [d.column for d in ir.dimensions] == ["segment", "sector"]


def test_the_resulting_ir_RUNS_and_reconciles_to_the_hand_counted_fixture(db):
    """End to end through the seam: a plan on one side, the fixture's hand-counted answer on the
    other. Without this the bridge could produce a well-typed IR that computes the wrong thing."""
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect

    create_pilot_tables(db)
    ir = plan_to_execution_ir(_grounded(), _inputs())
    rows = run_analysis(db, ir, dialect=PostgresDialect())
    assert len(rows) == EXPECTED["customer_rows"]
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]


@pytest.mark.parametrize("word,expected", [
    ("decrease", Comparison.DECREASED), ("increase", Comparison.INCREASED),
    ("change", Comparison.CHANGED)])
def test_the_comparison_vocabulary_maps(word, expected):
    ir = plan_to_execution_ir(_grounded(plan=_plan(comparison=word)), _inputs())
    assert ir.comparison is expected


# ── what the plan contract cannot express, refused by name ───────────────────────────────────────

def test_a_plan_whose_spine_IS_the_event_table_is_refused():
    """THE one. The IR's population spine must be a different table from the events, or the LEFT
    JOIN is pointless and the customer who fell to zero disappears — the exact case the whole
    analysis exists to catch. The plan carries a single `base_table_ref`, so this cannot be inferred
    and must not be guessed."""
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(_grounded(), _inputs(
            spine_binding=binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE)))
    assert exc.value.code == "SPINE_SAME_AS_EVENTS"


def test_a_plan_with_no_eligibility_policy_is_refused_here_not_deeper():
    """The IR refuses this too, but refusing at the seam names the PLAN as what is short, which is
    what a reviewer can act on."""
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(_grounded(), _inputs(eligibility=None))
    assert exc.value.code == "ELIGIBILITY_ABSENT"


def test_a_window_with_no_partition_values_is_refused():
    """A `Window` is 30 days; the executor reads partitions. Nothing in the plan says which."""
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(_grounded(), _inputs(window_partitions={"current": (CURRENT_MONTH,)}))
    assert exc.value.code == "WINDOW_NOT_PARTITION_ALIGNED"
    # `subject` is the catalog object a human would act on — the anchor column — matching
    # `Finding.subject`. Which WINDOW is short belongs in the message.
    assert exc.value.subject == "ftr::dpl_eib.tran_repos.tran_month"
    assert "previous" in str(exc.value)


def test_an_unsupported_measure_is_refused_with_the_op_named():
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(_grounded(plan=_plan(measure=Measure(op="sum",
                             logical_ref="ftr::dpl_eib.tran_repos.tran_amt"))), _inputs())
    assert exc.value.code == "MEASURE_UNSUPPORTED"
    assert "sum" in str(exc.value)


def test_an_UNANSWERABLE_plan_is_never_translated():
    """Grounding already refused it — a refusal means the plan could not be EXPRESSED. Translating it
    anyway would run a question the catalog said it cannot answer."""
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(
            _grounded(answerable=False, refusals=(("COLUMN_ABSENT", "ftr::x.y.z"),)), _inputs())
    assert exc.value.code == "PLAN_NOT_ANSWERABLE"


def test_a_comparison_needs_exactly_two_windows():
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(_grounded(plan=_plan(windows=_plan().windows[:1])), _inputs())
    assert exc.value.code == "COMPARISON_NEEDS_TWO_WINDOWS"


# ── findings travel, they do not block ───────────────────────────────────────────────────────────

def test_a_plan_carrying_FINDINGS_still_translates():
    """"Findings do not block" is the planning contract's central rule (`plan.py`): the agent answers
    and discloses what the answer rests on. A bridge that refused on findings would quietly convert
    every disclosure into a dead end on a young catalog, where findings are expected."""
    from featuregen.analysis.plan import Finding

    grounded = _grounded(findings=(
        Finding(code="CURRENCY_MIXED", subject="ftr::dpl_eib.tran_repos.tran_amt"),
        Finding(code="JOIN_IDENTITY_UNCONFIRMED", subject="ftr::dpl_eib.tran_repos.cif_id")))
    ir = plan_to_execution_ir(grounded, _inputs())
    assert ir.comparison is Comparison.DECREASED


def test_cross_catalog_production_analysis_requires_exact_directional_realization():
    grounded = _grounded(plan=_plan(join_refs=("bridge-fact-1",)))
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(grounded, _inputs())
    assert exc.value.code == "JOIN_REALIZATION_ABSENT"

    realization = _realization_current(_inventory(INVENTORY))
    ir = plan_to_execution_ir(
        grounded,
        _inputs(bridge_realizations=(realization,)),
    )
    assert ir.bridge_realization_dependencies == ((
        realization.revision.realization_revision_id,
        realization.revision.dependency_snapshot_id,
    ),)


# ── every refusal is actionable ontology evidence ────────────────────────────────────────────────

def test_every_bridge_refusal_maps_to_a_real_gap_code():
    """A refusal nobody can act on is a stack trace. Each maps into the learning vocabulary so a
    blocked question reaches the queue built for it."""
    assert BRIDGE_REFUSAL_TO_GAP
    for refusal, (gap_code, _action) in BRIDGE_REFUSAL_TO_GAP.items():
        assert gap_code in GAP_CODES, f"{refusal} -> unknown gap {gap_code}"


def test_the_gap_codes_that_had_no_producer_now_have_one():
    """`POPULATION_UNRESOLVED`, `PHYSICAL_BINDING_MISSING` and `POINT_IN_TIME_RULE_MISSING` were
    declared and unreachable. If a later change orphans them again this test says so."""
    produced = {gap for gap, _ in BRIDGE_REFUSAL_TO_GAP.values()}
    assert {"POPULATION_UNRESOLVED", "POINT_IN_TIME_RULE_MISSING"} <= produced


# ── the declared population must BIND ────────────────────────────────────────────────────────────

def test_a_spine_binding_that_is_not_the_DECLARED_population_is_refused():
    """A population declared and then substituted is worse than one never declared: the audit trail
    says a person chose it, and the number is from somewhere else.

    `spine.py`'s doctrine is that the declaration chooses the source and governed facts may only
    validate it. That only means anything if the declaration is checked against what actually runs.
    """
    declared = _plan(population_table_ref="ftr::dpl_eib.customer_master",
                     population_key_ref="ftr::dpl_eib.customer_master.cif_id")
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(_grounded(plan=declared),
                             _inputs(spine_binding=binding(CUSTOMER_SCHEMA, DIMENSION_TABLE)))
    assert exc.value.code == "SPINE_NOT_THE_DECLARED_POPULATION"


def test_a_spine_binding_that_MATCHES_the_declaration_is_accepted():
    declared = _plan(population_table_ref="ftr::dpl_eib.customer_master",
                     population_key_ref="ftr::dpl_eib.customer_master.cif_id")
    ir = plan_to_execution_ir(_grounded(plan=declared), _inputs())
    assert ir.spine.binding.identity.table == CUSTOMER_TABLE


def test_an_UNDECLARED_population_still_works_with_the_selection_flag_OFF():
    """CHANGE OF INTENT, Release-B Task 8 — this test was
    `test_an_UNDECLARED_population_still_works_for_a_caller_that_supplies_its_own_spine`, and it
    pinned the fail-open as intended: "the check is on the DECLARATION, not a new requirement to
    declare — a caller assembling ExecutionInputs directly is unaffected."

    That was defensible while NOTHING could resolve a population. Refusing would have left the
    executor's own tests and any batch caller with no way to run at all, so the bridge accepted
    whatever spine it was handed and the plan's own DoD item ("enforce the declaration again at the
    plan-to-execution bridge") was simply not true. Release B supplies the resolution — an explicit
    declaration or a serving policy — so the fallback now has an alternative and the honest
    behaviour is to refuse.

    The old acceptance survives EXACTLY, as the flag-off half of the matrix: a tree that has not
    enabled `FEATUREGEN_SOURCE_TEMPORAL_SELECTION` behaves byte-identically to before.
    """
    ir = plan_to_execution_ir(_grounded(), _inputs())
    assert ir.spine.binding.identity.table == CUSTOMER_TABLE


def test_an_UNDECLARED_population_REFUSES_with_the_selection_flag_ON(monkeypatch):
    """The population hole, closed. A caller-supplied spine is no longer its own authority."""
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    with pytest.raises(BridgeRefusal) as exc:
        plan_to_execution_ir(_grounded(), _inputs())
    assert exc.value.code == SELECTION_POPULATION_UNDECLARED


def test_a_DECLARED_population_is_unaffected_by_the_flag(monkeypatch):
    """The new refusal is about the ABSENCE of a declaration, not a new hoop for one that exists."""
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    declared = _plan(population_table_ref="ftr::dpl_eib.customer_master",
                     population_key_ref="ftr::dpl_eib.customer_master.cif_id")
    ir = plan_to_execution_ir(_grounded(plan=declared), _inputs())
    assert ir.spine.binding.identity.table == CUSTOMER_TABLE


def test_the_flag_is_fail_closed_on_its_dependency(monkeypatch):
    """D8: requesting SOURCE_TEMPORAL_SELECTION without DATASET_PROFILES is an INVALID
    configuration, not a half-enabled path — so the bridge stays on its flag-off behaviour."""
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    ir = plan_to_execution_ir(_grounded(), _inputs())
    assert ir.spine.binding.identity.table == CUSTOMER_TABLE


def test_the_new_population_refusal_maps_to_the_same_gap_the_selector_uses():
    """One thing to decide, reached from two directions."""
    from featuregen.data_agent.learning import REFUSAL_TO_GAP

    assert (BRIDGE_REFUSAL_TO_GAP[SELECTION_POPULATION_UNDECLARED]
            == REFUSAL_TO_GAP[SELECTION_POPULATION_UNDECLARED])
