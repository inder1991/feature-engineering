"""Shared seeds for B2's binding-chain and adoption suites (migrations 1135/1136).

Two kinds of helper, deliberately separated:

* the PRE-EXISTING platform rows a binding needs a parent for — the served option, the formula
  draft, the target reading, the selection, the build set. These are written with raw SQL against
  the tables their own migrations created, because B2 adds no producer for them (R15's selection
  producer is a later task, and the option/draft producers are the running platform's);
* the STEP-3 identity contracts, built exactly as B1's suite builds them, so the two suites test one
  vocabulary rather than two.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from featuregen.overlay.upload.bridge_realization import (
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.bridge_realization_proposal import FEATURE_GENERATION_PURPOSE
from featuregen.overlay.upload.execution_context import ensure_execution_context_revision
from featuregen.overlay.upload.planner.logical_plan_v2 import (
    DrivingTimeRoleV1,
    IntervalBoundaryPolicyV1,
    KnowledgeTimeBasisV2,
    LogicalFeaturePlanV2,
    LogicalOperandBindingV1,
    LogicalPlanProvenanceV1,
    LogicalRelationshipSegmentV1,
    LogicalTemporalJoinSemanticsV1,
    StaticLinkMeaningV1,
    UnmatchedRowMeaningV1,
)
from featuregen.overlay.upload.planner.physical_plan_v1 import (
    BlankKeyBehaviorV1,
    CaseNormalizationV1,
    CompositeKeyOrderingV1,
    CoverageDenominatorV1,
    CoverageNumeratorV1,
    JoinKeyNormalizationPolicy,
    JoinOrientationV1,
    JoinValidationPolicyRevisionV1,
    LeadingZeroPolicyV1,
    NullKeyBehaviorV1,
    PhysicalExecutionPlanV1,
    PhysicalJoinSegmentV1,
    PhysicalTemporalJoinBindingV1,
    SnapshotSelectionRuleV1,
    UnmatchedRowBehaviorV1,
    WhitespaceNormalizationV1,
)
from featuregen.overlay.upload.planner.render_profile import RenderProfileV1

CUST = "cib::public.bo_cib_customer"
TXN = "ftr::public.comp_financial_tran_repos_dly"


# ── the step-3 contracts ───────────────────────────────────────────────────────────────────────
def semantics(**over) -> LogicalTemporalJoinSemanticsV1:
    base = dict(
        effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        knowledge_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        driving_time_role=DrivingTimeRoleV1.CUTOFF_PARAMETER,
        interval_boundary_policy=IntervalBoundaryPolicyV1.CLOSED_OPEN,
        unmatched_row_meaning=UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
        static_link_meaning=StaticLinkMeaningV1.REFUSE)
    base.update(over)
    return LogicalTemporalJoinSemanticsV1(**base)


def logical_plan(**over) -> LogicalFeaturePlanV2:
    base = dict(
        canonical_definition_content_hash="c" * 64,
        canonical_definition_revision_id="cdr_1",
        operation="sum_window_delta",
        operand_bindings=(
            LogicalOperandBindingV1(role="amount",
                                    logical_column_ref=f"{TXN}.actual_tran_amt_aed",
                                    governed_semantic_revision_id="sem_amount_1"),),
        output_grain_key_refs=(f"{CUST}.cust_num",),
        selected_parameters=(("new_customer_days", 180), ("window_days", 30)),
        relationship_path=(
            LogicalRelationshipSegmentV1(left_endpoint_refs=(f"{CUST}.cust_num",),
                                         right_endpoint_refs=(f"{TXN}.cif_id",),
                                         temporal_semantics=semantics()),),
        formula_policy_identities=(("direction_value_map", "pol_dir_1"),),
        provenance=LogicalPlanProvenanceV1())
    base.update(over)
    return LogicalFeaturePlanV2(**base)


def normalization(**over) -> JoinKeyNormalizationPolicy:
    base = dict(
        whitespace=WhitespaceNormalizationV1.TRIM,
        case_handling=CaseNormalizationV1.PRESERVE,
        leading_zeros=LeadingZeroPolicyV1.PRESERVE,
        declared_type_coercions=(("varchar(150)", "string"),),
        blank_key_behavior=BlankKeyBehaviorV1.NEVER_MATCH,
        nulls_never_match=True,
        composite_key_ordering=CompositeKeyOrderingV1.DECLARED_PAIR_ORDER)
    base.update(over)
    return JoinKeyNormalizationPolicy(**base)


def temporal_binding(**over) -> PhysicalTemporalJoinBindingV1:
    base = dict(
        dataset_temporal_policy_revision_id="dtp_" + "d" * 64,
        effective_from_column_ref=None,
        effective_to_column_ref=None,
        availability_or_knowledge_time_column_ref=f"{CUST}.business_dt",
        cutoff_parameter_ref="report_cutoff",
        source_binding_revision_id="sbr_cust_1",
        tie_break_column_refs=(f"{CUST}.business_dt",))
    base.update(over)
    return PhysicalTemporalJoinBindingV1(**base)


def join_policy(**over) -> JoinValidationPolicyRevisionV1:
    base = dict(
        null_key_behavior=NullKeyBehaviorV1.EXCLUDE_ROW,
        unmatched_row_behavior=UnmatchedRowBehaviorV1.PRESERVE_LEFT_NULL,
        coverage_numerator=CoverageNumeratorV1.MATCHED_LEFT_ROWS,
        coverage_denominator=CoverageDenominatorV1.NON_NULL_KEY_LEFT_ROWS,
        minimum_coverage_ratio=0.95,
        orientation=JoinOrientationV1.LEFT_DRIVING,
        max_matches_per_left_row=1,
        snapshot_selection_rule=SnapshotSelectionRuleV1.LATEST_AT_OR_BEFORE_CUTOFF,
        applies_to_final_grain_aggregate=True,
        fan_out_control_operator=None,
        declared_by="user:ascoe",
        declared_at="2026-08-24T00:00:00Z")
    base.update(over)
    return JoinValidationPolicyRevisionV1(**base)


def join_segment(**over) -> PhysicalJoinSegmentV1:
    base = dict(
        realization_revision_id="bjr_1",
        column_pairs=(ColumnPairV1(f"{CUST}.cust_num", f"{TXN}.cif_id"),),
        predicates=(FixedValueReferencePredicateV1(predicate_id="pred_1",
                                                   logical_column_ref=f"{CUST}.business_dt",
                                                   value_ref="report_cutoff"),),
        directional_cardinality=DirectionalCardinalityVerdictV1.unknown(),
        realization_content_hash="e" * 64,
        realization_dependency_hash="f" * 64,
        key_normalization=normalization(),
        temporal_binding=temporal_binding())
    base.update(over)
    return PhysicalJoinSegmentV1(**base)


def physical_plan(*, context_id: str, logical_digest_ref: str, policy_id: str,
                  **over) -> PhysicalExecutionPlanV1:
    base = dict(
        logical_digest_ref=logical_digest_ref,
        execution_context_revision_id=context_id,
        source_binding_revisions=((CUST, "sbr_cust_1"), (TXN, "sbr_txn_1")),
        segments=(join_segment(),),
        join_validation_policy_revision_id=policy_id)
    base.update(over)
    return PhysicalExecutionPlanV1(**base)


def render_profile(**over) -> RenderProfileV1:
    base = dict(engine="pyspark_kedro", compiler_version="compiler-1.0.0",
                renderer_version="renderer-1.0.0",
                template_versions=(("node", "1.0.0"), ("pipeline", "2.0.0")))
    base.update(over)
    return RenderProfileV1(**base)


def execution_context(db, *, environment_id="env-uat", tier=ExecutionTier.SANDBOX) -> str:
    return ensure_execution_context_revision(
        db, environment_id=environment_id, execution_tier=tier,
        purpose=FEATURE_GENERATION_PURPOSE)


# ── the platform rows a binding needs a parent for ─────────────────────────────────────────────
def seed_candidate(db, *, considered="cr_1") -> str:
    """The three rows a considered revision cannot exist without (1021/1027/1116): an intent, a
    generation run, and the frozen considered revision itself. ``formula_draft`` foreign-keys onto
    the last of them, so "give the draft a real candidate" is three rows, not one."""
    intent, run = f"{considered}-int", f"{considered}-run"
    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES (%s, 'h', 'hypothesis') ON CONFLICT DO NOTHING", (intent,))
    db.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
        "VALUES (%s, %s, '{\"subject\": \"b2-binding-chain\"}'::jsonb, '{}') "
        "ON CONFLICT DO NOTHING", (run, intent))
    db.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "  generation_run_id, considered_json, considered_content_hash, canonicalization_version) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'cch', 'v1') ON CONFLICT DO NOTHING",
        (considered, intent, run))
    return considered


def seed_option(db, *, considered="cr_1", option="opt_1", planning_request_hash="prh_1",
                planned=False, decision_id=None) -> tuple[str, str]:
    """One served option. ``planned=True`` sets 1135's marker, which arms the totality trigger."""
    seed_candidate(db, considered=considered)
    db.execute(
        "INSERT INTO semantic_option_decision (decision_id, considered_revision_id, option_id, "
        "  generation_run_id, source_definition_id, generation_source, computation_kind, "
        "  planning_request_hash, binding_state, readiness, requires_logical_plan_binding) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (decision_id or f"dec_{considered}_{option}", considered, option, "run_1", "sd_1",
         "llm_intent", "window_aggregate", planning_request_hash, "bound", "ready", planned))
    return considered, option


def seed_draft(db, *, considered="cr_1", option="opt_1", planning_request_hash="prh_1",
               draft_id="fd_1", formula_content_hash="fch_1", state="READY") -> str:
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "  planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
        "  definition_revision, formula_identity_hash, state, formula_content_hash, formula_json, "
        "  requested_by, requested_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (draft_id, considered, option, planning_request_hash, "csh_1", "ach_1", "defrev_1",
         f"fih_{draft_id}", state, formula_content_hash, Jsonb({"operation": "sum"}),
         "user:ascoe", "2026-08-24T00:00:00Z"))
    return draft_id


def seed_target_reading(db, *, revision_id="trr_1", intent_id="intent_1") -> str:
    db.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES (%s, %s, 'exploration', %s)", (revision_id, intent_id, f"ch_{revision_id}"))
    return revision_id


def seed_selection(db, *, reading, considered="cr_1", option="opt_1",
                   planning_request_hash="prh_1", binding_plan_hash="bph_1",
                   revision_id="fsr_1") -> str:
    db.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "  considered_revision_id, option_id, decision_id, planning_request_hash, "
        "  binding_plan_hash, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (revision_id, reading, considered, option, f"dec_{considered}_{option}",
         planning_request_hash, binding_plan_hash, f"ch_{revision_id}"))
    return revision_id


def seed_build_set(db, *, reading, revision_id="bsr_1") -> str:
    db.execute(
        "INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
        "  declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (revision_id, reading, f"dh_{revision_id}", Jsonb({"cadence": "on_demand"}),
         f"ch_{revision_id}", "user:ascoe", "2026-08-24T00:00:00Z"))
    return revision_id


def commit_checks(db) -> None:
    """Force the DEFERRED constraint triggers to run NOW.

    The suite's connection is rolled back on teardown and never commits, so a trigger that fires at
    COMMIT would otherwise never fire at all. ``SET CONSTRAINTS ALL IMMEDIATE`` checks every pending
    deferred constraint at once — the same work COMMIT would do, at a point a test can observe."""
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")


def transaction_boundary(db) -> None:
    """Simulate one COMMIT and the start of the next transaction, on a single connection.

    ``SET CONSTRAINTS ALL IMMEDIATE`` runs every pending deferred check and DISCHARGES it, exactly
    as a COMMIT would; ``SET CONSTRAINTS ALL DEFERRED`` then restores deferral so whatever the test
    does next queues its own events afresh. What this reproduces is the property that matters for a
    cross-transaction construction: **a deferred trigger that has already passed never runs again.**

    ▲ WHY NOT TWO REAL CONNECTIONS. A second session would have to see COMMITTED rows, and every
    table involved here (`semantic_option_decision`, `selection_formula_binding`, the binding chain)
    is append-only with no DELETE and no TRUNCATE — so a genuine two-transaction fixture would leak
    permanent rows into a persistent ``FEATUREGEN_TEST_DSN`` database that nothing could ever clean
    up. The fidelity gap is stated rather than hidden: this does not prove isolation behaviour, and
    it is not meant to. It proves the thing the construction turns on."""
    db.execute("SET CONSTRAINTS ALL IMMEDIATE")
    db.execute("SET CONSTRAINTS ALL DEFERRED")
