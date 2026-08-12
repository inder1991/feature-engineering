"""Release-B Task 8 — the selector, wired into planning, clarification and learning.

The flag matrix first: with ``FEATUREGEN_SOURCE_TEMPORAL_SELECTION`` off, a ``GroundedPlan`` is the
one this release found, field for field. With it on, the plan carries its source and row decisions,
its refusals become the SAME clarification questions Task 7 wrote, and each refusal that is an
actionable gap lands in the learning queue under a STABLE request id — so asking a blocked question
three times is one thing to decide, not three.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.analysis.clarify import clarifications_for_codes
from featuregen.analysis.grounding import (
    ground_analysis_plan,
    plan_cutoff_value_ref,
    record_selection_gaps,
    resolve_plan_selections,
)
from featuregen.analysis.intent import IntentCandidates
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure, Window
from featuregen.data_agent.binding_store import declare_catalog_engine, record_connection
from featuregen.data_agent.connection import DataSourceConnectionV1
from featuregen.data_agent.learning import open_gaps
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.serving_policy_store import publish_serving_policy
from featuregen.overlay.upload.source_selection import (
    SELECTION_AUTHORITY_INSUFFICIENT,
    SELECTION_BINDING_MISSING,
    SELECTION_POPULATION_UNDECLARED,
    SELECTION_SOURCE_AMBIGUOUS,
    TEMPORAL_MODEL_UNKNOWN,
    TEMPORAL_SCD_OVERLAP,
    DatasetNeedRole,
    DatasetServingPolicyRevisionV1,
    ServingPurpose,
    human_declaration_provenance,
)
from featuregen.overlay.upload.source_selector import PROPOSED_AUTHORITY_USED, SelectionRefusalV1
from featuregen.overlay.upload.temporal_policy import (
    DatasetTemporalPolicyRevisionV1,
    TemporalSelectionKind,
)
from featuregen.overlay.upload.temporal_policy_store import publish_temporal_policy

ADMIN = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))

_SRC = "bank"
_SCHEMA = "dpl_eib"
CUST = normalize_ref(_SRC, _SCHEMA, "customer_master")
CUST_ODS = normalize_ref(_SRC, _SCHEMA, "customer_ods")
TRAN = normalize_ref(_SRC, _SCHEMA, "tran_repos")
SEG = normalize_ref(_SRC, _SCHEMA, "customer_segment")
SEG_COL = normalize_ref(_SRC, _SCHEMA, "customer_segment", "segment")
SEG_FROM = normalize_ref(_SRC, _SCHEMA, "customer_segment", "effective_from")
SEG_TO = normalize_ref(_SRC, _SCHEMA, "customer_segment", "effective_to")
SEG_FLAG = normalize_ref(_SRC, _SCHEMA, "customer_segment", "is_current")
TRAN_KEY = normalize_ref(_SRC, _SCHEMA, "tran_repos", "cif_id")
TRAN_DATE = normalize_ref(_SRC, _SCHEMA, "tran_repos", "posted_at")

CUTOFF_REF = "report_cutoff_param"
SNAPSHOT = "snap-1"


@pytest.fixture
def flagged(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_DATASET_PROFILES", "1")
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")


@pytest.fixture
def catalog(db):
    build_graph(db, _SRC, [
        CanonicalRow(_SRC, "customer_master", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "customer_ods", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "tran_repos", "cif_id", "text"),
        CanonicalRow(_SRC, "tran_repos", "posted_at", "timestamp", as_of=True),
        CanonicalRow(_SRC, "customer_segment", "cif_id", "text", is_grain=True),
        CanonicalRow(_SRC, "customer_segment", "segment", "text"),
        CanonicalRow(_SRC, "customer_segment", "effective_from", "timestamp"),
        CanonicalRow(_SRC, "customer_segment", "effective_to", "timestamp"),
        CanonicalRow(_SRC, "customer_segment", "is_current", "boolean"),
    ])
    db.execute("UPDATE graph_node SET schema_name = %s WHERE catalog_source = %s", (_SCHEMA, _SRC))
    record_connection(db, DataSourceConnectionV1(
        connection_id="edp-1", environment_id="dev", kind="hive", host="hs2.internal", port=10000,
        auth_mechanism="kerberos", secret_ref="vault://featuregen/hive",
        execution_principal="svc_ro", allowed_schemas=frozenset({_SCHEMA}), active=True),
        tier="edp", database_name="edp_cluster")
    declare_catalog_engine(db, catalog_source=_SRC, engine="hive", tier="edp", declared_by="p")
    return db


def _temporal(db, **over) -> None:
    kw = dict(dataset_logical_ref=SEG, temporal_storage_model=TemporalStorageModel.SCD2,
              current_selection=TemporalSelectionKind.CURRENT_RECORD,
              historical_selection=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
              effective_from_ref=SEG_FROM, effective_to_ref=SEG_TO, current_flag_ref=SEG_FLAG,
              provenance=human_declaration_provenance(producer_ref="user:priya"))
    kw.update(over)
    publish_temporal_policy(db, DatasetTemporalPolicyRevisionV1(**kw),
                            expected_pointer_version=0, actor="user:priya")


def _plan(**over) -> AnalysisPlanV1:
    kw = dict(
        question="customers whose transaction count decreased last month, by segment",
        entity="customer", entity_ref=TRAN_KEY, base_table_ref=TRAN,
        measure=Measure(op="count"),
        windows=(Window(anchor_ref=TRAN_DATE, length_days=30, offset_days=0),
                 Window(anchor_ref=TRAN_DATE, length_days=30, offset_days=30)),
        dimensions=(Dimension(logical_ref=SEG_COL),),
        comparison="decrease",
        population_table_ref=CUST,
        population_key_ref=normalize_ref(_SRC, _SCHEMA, "customer_master", "cif_id"))
    kw.update(over)
    return AnalysisPlanV1(**kw)


def _resolve(db, plan=None, **over):
    return resolve_plan_selections(db, plan or _plan(), cutoff_value_ref=CUTOFF_REF, **over)


# ── the flag matrix ─────────────────────────────────────────────────────────────────────────────


def test_with_the_flag_OFF_a_grounded_plan_is_exactly_what_it_was(catalog, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    grounded = ground_analysis_plan(catalog, _plan())
    assert grounded.selections is None
    assert grounded.answerable is True


def test_with_the_flag_OFF_nothing_is_read_and_no_binding_is_pinned(catalog, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", raising=False)
    _resolve(catalog)
    rows = catalog.execute(
        "SELECT count(*) FROM physical_dataset_binding_revision").fetchone()[0]
    assert rows == 0


def test_the_flag_is_fail_closed_on_its_dependency(catalog, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SOURCE_TEMPORAL_SELECTION", "1")
    monkeypatch.delenv("FEATUREGEN_DATASET_PROFILES", raising=False)
    assert _resolve(catalog) is None


# ── with the flag on: the decisions ride the plan ───────────────────────────────────────────────


def test_the_plan_carries_its_source_and_row_decisions(catalog, flagged):
    _temporal(catalog)
    selections = _resolve(catalog)
    chosen = {s.need.need_role.value: s.selected_dataset_ref for s in selections.source_selections}
    assert chosen == {"population": CUST, "event_source": TRAN, "dimension_source": SEG}
    assert selections.resolved
    # The dimension source — and ONLY it — gets a row decision: "which row classifies this
    # customer" is the question the renderer used to answer by itself.
    assert [r.dataset_logical_ref for r in selections.row_selections] == [SEG]
    row = selections.row_selections[0]
    assert row.selection_kind is TemporalSelectionKind.VALID_AT_REPORT_CUTOFF
    assert row.cutoff_value_ref == CUTOFF_REF


def test_a_period_over_period_question_reads_the_HISTORICAL_row_rule(catalog, flagged):
    """"Decreased last month" is asked about a window that has closed. Today's segment would
    reattribute it — the defect `dimensions` exists to prevent."""
    _temporal(catalog)
    historical = _resolve(catalog).row_selections[0]
    current = _resolve(catalog, plan=_plan(comparison="")).row_selections[0]
    assert historical.selection_kind is TemporalSelectionKind.VALID_AT_REPORT_CUTOFF
    assert current.selection_kind is TemporalSelectionKind.CURRENT_RECORD


def test_every_selection_records_what_else_was_considered(catalog, flagged):
    _temporal(catalog)
    publish_serving_policy(catalog, DatasetServingPolicyRevisionV1(
        entity_id="customer", need_role=DatasetNeedRole.POPULATION,
        serving_purpose=ServingPurpose.ANALYTICAL,
        eligible_dataset_refs=(CUST, CUST_ODS), preferred_dataset_refs=(CUST,),
        provenance=human_declaration_provenance(producer_ref="user:priya")),
        expected_pointer_version=0, actor="user:priya")
    selections = _resolve(catalog, plan=_plan(population_table_ref="", population_key_ref=""))
    population = [s for s in selections.source_selections
                  if s.need.need_role is DatasetNeedRole.POPULATION][0]
    assert {c.dataset_ref for c in population.considered_candidates} == {CUST, CUST_ODS}


def test_an_undeclared_population_is_a_refusal_that_does_not_make_the_plan_unanswerable(
        catalog, flagged):
    """Grounding DISCLOSES; the execution bridge is the hard gate. "Nobody declared this" is a
    question for a person, not a plan that could not be expressed."""
    _temporal(catalog)
    grounded = ground_analysis_plan(
        catalog, _plan(population_table_ref="", population_key_ref=""),
        cutoff_value_ref=CUTOFF_REF)
    assert grounded.answerable is True
    assert SELECTION_POPULATION_UNDECLARED in grounded.selections.refusal_codes


def test_a_dimension_source_with_no_temporal_policy_refuses(catalog, flagged):
    selections = _resolve(catalog)
    assert TEMPORAL_MODEL_UNKNOWN in selections.refusal_codes
    assert selections.row_selections == ()


def _proposed_authorities(catalog) -> None:
    """Two candidates a GOVERNED authority cannot separate — only an LLM PROPOSAL can."""
    for table in ("customer_master", "customer_ods"):
        ref = normalize_ref(_SRC, _SCHEMA, table)
        record_field_evidence(
            catalog, logical_ref=ref, field_name="authority_role",
            proposed_value="system_of_record" if table == "customer_master" else "derived",
            producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
            producer_ref="llm-test", source_snapshot_id=SNAPSHOT,
            input_hash=field_input_hash(logical_ref=ref, field_name="authority_role",
                                        material=f"{table}:llm:proposed"))


def test_the_DEFAULT_tier_is_PRODUCTION_and_will_not_rank_on_a_proposal(catalog, flagged):
    """CHANGE OF INTENT (Task-8 review, F2). `execution_tier` defaulted to "sandbox" and NOTHING
    passed a tier, so every real selection ran under the tier that may rank on an unconfirmed LLM
    classification — the governance default moved production -> sandbox on the production path.
    The tier you get by saying nothing is now the strict one."""
    _temporal(catalog)
    _proposed_authorities(catalog)
    selections = _resolve(catalog, plan=_plan(base_table_ref=""),
                          candidate_dataset_refs=(CUST, CUST_ODS))
    assert selections.warnings == ()
    assert SELECTION_AUTHORITY_INSUFFICIENT in selections.refusal_codes
    assert not selections.resolved


def test_a_proposed_authority_ranks_only_when_SANDBOX_is_ASKED_FOR(catalog, flagged):
    """Visible, never a log line (§5.2: the authority travels with the value) — and reachable only
    by naming the tier, which is what makes sandbox opt-in rather than the default."""
    _temporal(catalog)
    _proposed_authorities(catalog)
    selections = _resolve(catalog, plan=_plan(base_table_ref=""),
                          candidate_dataset_refs=(CUST, CUST_ODS), execution_tier="sandbox")
    assert selections.warnings == (PROPOSED_AUTHORITY_USED,)
    event = [s for s in selections.source_selections
             if s.need.need_role is DatasetNeedRole.EVENT_SOURCE][0]
    assert event.selected_dataset_ref == CUST


def test_the_population_is_refused_in_either_tier_candidates_never_decide_it(catalog, flagged):
    _temporal(catalog)
    _proposed_authorities(catalog)
    production = _resolve(catalog, plan=_plan(base_table_ref="", population_table_ref="",
                                              population_key_ref=""),
                          candidate_dataset_refs=(CUST, CUST_ODS), execution_tier="production")
    assert production.warnings == ()
    assert SELECTION_POPULATION_UNDECLARED in production.refusal_codes
    assert SELECTION_AUTHORITY_INSUFFICIENT in production.refusal_codes
    sandbox = _resolve(catalog, plan=_plan(base_table_ref="", population_table_ref="",
                                           population_key_ref=""),
                       candidate_dataset_refs=(CUST, CUST_ODS), execution_tier="sandbox")
    assert SELECTION_POPULATION_UNDECLARED in sandbox.refusal_codes


# ── the row rule is present, or its absence is VISIBLE ──────────────────────────────────────────


def test_a_historical_plan_with_no_cutoff_REFUSES_instead_of_silently_having_no_row_rule(
        catalog, flagged):
    """THE REVIEW'S PROBE (F1). The resolver raised here, a blanket `except SelectionError:
    continue` in this module erased it, and the plan came back with no row rule, no refusal, and
    `resolved` True: the agent silently had no row rule while claiming resolution."""
    _temporal(catalog)
    selections = resolve_plan_selections(catalog, _plan(), cutoff_value_ref=None)
    assert selections.row_selections == ()
    assert TEMPORAL_MODEL_UNKNOWN in selections.refusal_codes
    assert not selections.resolved


def test_the_cutoff_ref_is_DERIVED_from_the_plans_own_time_context(catalog, flagged):
    """What the route threads: a plan's windows are measured back from the instant it is asked as
    of, and that instant IS the report cutoff the dimension row rule reads."""
    _temporal(catalog)
    assert plan_cutoff_value_ref(_plan()) == "report_cutoff"
    selections = resolve_plan_selections(
        catalog, _plan(), cutoff_value_ref=plan_cutoff_value_ref(_plan()))
    assert [r.cutoff_value_ref for r in selections.row_selections] == ["report_cutoff"]
    assert selections.resolved


def test_a_plan_with_NO_time_context_has_no_cutoff_to_derive_and_says_so(catalog, flagged):
    """No windows means no instant to be "as of". The honest answer is that there is nothing to
    derive — and for a historical question that is a refusal, not a silent absence."""
    _temporal(catalog)
    timeless = _plan(windows=())
    assert plan_cutoff_value_ref(timeless) is None
    selections = resolve_plan_selections(
        catalog, timeless, cutoff_value_ref=plan_cutoff_value_ref(timeless))
    assert TEMPORAL_MODEL_UNKNOWN in selections.refusal_codes
    assert not selections.resolved


def test_a_preview_with_no_decisions_and_no_refusals_is_NEVER_resolved():
    """The shape a swallowed exception leaves behind. "Nothing complained" is not "everything was
    decided", and reporting the first as the second is what made the defect invisible."""
    from featuregen.analysis.plan import SelectionPreviewV1

    assert SelectionPreviewV1().resolved is False
    assert SelectionPreviewV1(needs_considered=3).resolved is False


def test_an_unaddressable_ref_is_a_typed_refusal_not_a_skipped_need(catalog, flagged):
    """A ref the selection contracts cannot address — no schema half of the physical address, and
    no catalog row that could supply one — was the ONE thing that legitimately raised here. It is
    the refusal it always was, so the need is never silently absent from the preview."""
    selections = resolve_plan_selections(
        catalog, _plan(population_table_ref="bank::no_such_table"),
        cutoff_value_ref=CUTOFF_REF)
    assert SELECTION_BINDING_MISSING in selections.refusal_codes
    assert not selections.resolved


def test_a_FLATTENED_ref_is_qualified_from_the_catalog_rather_than_refused(catalog, flagged):
    """The two spellings, joined. Retrieval offers `source::table.column` and the model plans in it;
    the selection contracts require `source::schema.table`. Both are right and nothing joined them,
    so EVERY LLM-planned question refused SELECTION_BINDING_MISSING at every need — the seal and the
    execute surface were unreachable from the planning route.

    The schema is READ from `graph_node`, the same lookup `resolve_table` already makes to build the
    binding this ref is addressed through, so nothing new is decided."""
    selections = resolve_plan_selections(
        catalog, _plan(population_table_ref="bank::customer_master"),
        cutoff_value_ref=CUTOFF_REF)
    assert SELECTION_BINDING_MISSING not in selections.refusal_codes
    population = next(s for s in selections.source_selections
                      if s.need.need_role is DatasetNeedRole.POPULATION)
    assert population.selected_dataset_ref == CUST
    # The DECLARATION still travels as one: it was answered by a person naming a table, and the
    # schema the catalog supplied is the address, not a second choice.
    assert population.need.explicit_dataset_ref == CUST


def test_an_AMBIGUOUS_table_name_stays_unaddressable_rather_than_being_guessed(catalog, flagged):
    """Two schemas holding one table name: picking either would supply the one component of the
    address the catalog exists to supply — by guess — and then persist it as an immutable `pbr_`
    revision. Left unaddressable, it surfaces as the refusal that already routes to an operator."""
    catalog.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  schema_name) VALUES (%s, 'dpl_arc.customer_master.alt_id', 'column', "
        "  'customer_master', 'alt_id', 'dpl_arc')", (_SRC,))
    assert catalog.execute(
        "SELECT count(DISTINCT schema_name) FROM graph_node "
        "WHERE catalog_source = %s AND table_name = 'customer_master'", (_SRC,)).fetchone()[0] == 2

    selections = resolve_plan_selections(
        catalog, _plan(population_table_ref="bank::customer_master"),
        cutoff_value_ref=CUTOFF_REF)
    assert SELECTION_BINDING_MISSING in selections.refusal_codes


# ── refusals become the SAME questions Task 7 wrote ─────────────────────────────────────────────


def test_selection_refusals_render_through_the_existing_clarification_vocabulary(catalog, flagged):
    selections = _resolve(catalog, plan=_plan(population_table_ref="", population_key_ref=""))
    candidates = IntentCandidates(
        column_refs=frozenset({SEG_COL, TRAN_KEY}), table_refs=frozenset({CUST, CUST_ODS, SEG}),
        grain_refs=frozenset({TRAN_KEY}), as_of_refs=frozenset({TRAN_DATE}), labels={})
    questions = clarifications_for_codes(selections.refusal_codes, candidates)
    assert [q.code for q in questions][0] == SELECTION_POPULATION_UNDECLARED
    assert all(q.question for q in questions)
    # No question is worded as a failure (the no-"blocked" product rule).
    assert not any(word in q.question.lower()
                   for q in questions for word in ("blocked", "failed", "error"))


def test_the_population_question_still_outranks_every_row_question():
    candidates = IntentCandidates(
        column_refs=frozenset({SEG_COL}), table_refs=frozenset({CUST}),
        grain_refs=frozenset({SEG_COL}), as_of_refs=frozenset(), labels={})
    ordered = clarifications_for_codes(
        (TEMPORAL_MODEL_UNKNOWN, SELECTION_SOURCE_AMBIGUOUS, SELECTION_POPULATION_UNDECLARED),
        candidates)
    assert [q.code for q in ordered] == [
        SELECTION_POPULATION_UNDECLARED, SELECTION_SOURCE_AMBIGUOUS, TEMPORAL_MODEL_UNKNOWN]


# ── refusals become learning gaps, under a STABLE request id ────────────────────────────────────


def _record(db, selections, question="who decreased?"):
    return record_selection_gaps(db, selections, question=question,
                                 dependency_snapshot_id=SNAPSHOT, now=datetime.now(UTC))


def test_a_refusal_lands_in_the_learning_queue(catalog, flagged):
    selections = _resolve(catalog, plan=_plan(population_table_ref="", population_key_ref=""))
    assert _record(catalog, selections)
    gaps = {gap.code for gap in open_gaps(catalog)}
    assert "POPULATION_UNRESOLVED" in gaps


def test_asking_the_same_blocked_question_twice_is_ONE_thing_to_decide(catalog, flagged):
    """The `stable_analysis_request_id` precedent: `record_gap` dedupes on (request, gap,
    snapshot), so a minted id would make every retry a new row and demand would read 3 for one
    decision."""
    selections = _resolve(catalog, plan=_plan(population_table_ref="", population_key_ref=""))
    first = _record(catalog, selections)
    second = _record(catalog, selections)
    assert first == second
    demand = {gap.code: gap.blocked_requests for gap in open_gaps(catalog)}
    assert demand["POPULATION_UNRESOLVED"] == 1


def test_a_data_defect_records_nothing(catalog, flagged):
    """SCD overlap is REFUSED and SHOWN, and is deliberately not a gap: nobody is waiting to decide
    anything — two history rows claiming one key at one instant is a defect in the source."""
    overlap = SelectionRefusalV1(code=TEMPORAL_SCD_OVERLAP, subject_refs=(SEG,), detail="x")
    from featuregen.analysis.plan import SelectionPreviewV1

    assert _record(catalog, SelectionPreviewV1(refusals=(overlap,))) == ()
    assert open_gaps(catalog) == ()


def test_nothing_is_recorded_when_there_is_nothing_to_record(catalog, flagged):
    _temporal(catalog)
    assert _record(catalog, _resolve(catalog)) == ()
    assert _record(catalog, None) == ()
