"""B1 — persistence for the three-layer identity contracts (migration 1134).

What these tests pin, in the order the task states them:

* the migration applies (the session fixture applies all migrations, so the seven tables exist);
* ROUND-TRIP FIDELITY per layer — the loaded contract equals the original and recomputes the same
  digest through step 3's own function;
* IMMUTABILITY — UPDATE / DELETE / TRUNCATE all refuse on every table;
* CONTENT-ADDRESSED IDEMPOTENCY — same content, same id, one row;
* R2/R9's LAYER INDEPENDENCE at the persistence layer — persisting a second physical plan against
  one logical plan leaves the logical row, its digest and its recorded_at untouched;
* the DIGEST-DRIFT GUARD — a row whose stored digest disagrees with its stored content refuses to
  load, for every layer and for every chain stage;
* store discipline — typed refusals BEFORE any SQL (a probe "connection" proves it), and the
  store-validation-instead-of-FK decisions (A4's TRUNCATE-raiser discovery).
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from featuregen.overlay.upload.bridge_realization import (
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    FixedValueReferencePredicateV1,
)
from featuregen.overlay.upload.bridge_realization_proposal import FEATURE_GENERATION_PURPOSE
from featuregen.overlay.upload.execution_context import ensure_execution_context_revision
from featuregen.overlay.upload.planner.identity_chain import (
    build_compilation_digest,
    formula_binding_digest,
    generation_configuration_digest,
    logical_digest,
    member_compile_digest,
    member_execution_input_digest,
    physical_digest,
    render_digest,
    sealed_artifact_identity,
)
from featuregen.overlay.upload.planner.identity_store import (
    GENERATION_CONFIGURATION_ID_PREFIX,
    IDENTITY_DIGEST_ID_PREFIX,
    IDENTITY_DIGEST_STAGES,
    LOGICAL_PLAN_ID_PREFIX,
    LOGICAL_PROVENANCE_ID_PREFIX,
    MEMBER_OUTPUT_CONTRACT_ID_PREFIX,
    PHYSICAL_PLAN_ID_PREFIX,
    RENDER_PROFILE_ID_PREFIX,
    STAGE_BUILD_COMPILATION,
    STAGE_FORMULA_BINDING,
    STAGE_MEMBER_COMPILE,
    STAGE_MEMBER_EXECUTION_INPUT,
    STAGE_SEALED_ARTIFACT,
    IdentityPersistenceDefect,
    IdentityStoreConflict,
    ensure_generation_configuration,
    ensure_logical_feature_plan,
    ensure_member_output_contract,
    ensure_physical_execution_plan,
    ensure_render_profile,
    load_generation_configuration,
    load_identity_digest_record,
    load_logical_feature_plan,
    load_logical_plan_provenance,
    load_member_output_contract,
    load_physical_execution_plan,
    load_render_profile,
    logical_plan_provenance_ids,
    record_identity_digest,
    resolve_identity_digest,
)
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
from featuregen.overlay.upload.planner.render_profile import (
    GenerationConfigurationV1,
    MemberOutputContractV1,
    NullInputBehaviorV1,
    OverflowPolicyV1,
    RenderProfileV1,
    RoundingPolicyV1,
)

CUST = "cib::public.bo_cib_customer"
TXN = "ftr::public.comp_financial_tran_repos_dly"

TABLES = (
    "logical_feature_plan_revision",
    "logical_plan_provenance_record",
    "physical_execution_plan_revision",
    "render_profile_revision",
    "generation_configuration_revision",
    "member_output_contract_revision",
    "identity_digest_record",
)


class _NeverTouched:
    """A probe standing in for a connection: ANY attribute access fails the test. Handing this to
    the store proves a refusal happened BEFORE SQL, not merely before commit."""

    def __getattr__(self, name):  # pragma: no cover - reaching here IS the failure
        raise AssertionError(f"the store touched the connection ({name!r}) before validating")


# ── the step-3 contracts, built exactly as their own suite builds them ─────────────────────────
def _semantics(**over) -> LogicalTemporalJoinSemanticsV1:
    base = dict(
        effective_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        knowledge_time_basis=KnowledgeTimeBasisV2.AS_OF_CUTOFF,
        driving_time_role=DrivingTimeRoleV1.CUTOFF_PARAMETER,
        interval_boundary_policy=IntervalBoundaryPolicyV1.CLOSED_OPEN,
        unmatched_row_meaning=UnmatchedRowMeaningV1.JOINED_ATTRIBUTES_NOT_APPLICABLE,
        static_link_meaning=StaticLinkMeaningV1.REFUSE,
    )
    base.update(over)
    return LogicalTemporalJoinSemanticsV1(**base)


def _provenance(**over) -> LogicalPlanProvenanceV1:
    base = dict(
        hypothesis_text="Find newly onboarded CIB customers whose outgoing payments increased.",
        planning_request_hash="a" * 64,
        chooser_revision_id="chooser_7",
        menu_content_hash="b" * 64,
        display_text="Outgoing payment increase (30d vs prior 30d)",
    )
    base.update(over)
    return LogicalPlanProvenanceV1(**base)


def _logical(**over) -> LogicalFeaturePlanV2:
    base = dict(
        canonical_definition_content_hash="c" * 64,
        canonical_definition_revision_id="cdr_1",
        operation="sum_window_delta",
        operand_bindings=(
            LogicalOperandBindingV1(
                role="amount",
                logical_column_ref=f"{TXN}.actual_tran_amt_aed",
                governed_semantic_revision_id="sem_amount_1"),
            LogicalOperandBindingV1(
                role="direction",
                logical_column_ref=f"{TXN}.tran_dc",
                governed_semantic_revision_id="sem_dir_1"),
        ),
        output_grain_key_refs=(f"{CUST}.cust_num",),
        selected_parameters=(("new_customer_days", 180), ("window_days", 30)),
        relationship_path=(
            LogicalRelationshipSegmentV1(
                left_endpoint_refs=(f"{CUST}.cust_num",),
                right_endpoint_refs=(f"{TXN}.cif_id",),
                temporal_semantics=_semantics(),
            ),
        ),
        formula_policy_identities=(("direction_value_map", "pol_dir_1"),),
        provenance=_provenance(),
    )
    base.update(over)
    return LogicalFeaturePlanV2(**base)


def _normalization(**over) -> JoinKeyNormalizationPolicy:
    base = dict(
        whitespace=WhitespaceNormalizationV1.TRIM,
        case_handling=CaseNormalizationV1.PRESERVE,
        leading_zeros=LeadingZeroPolicyV1.PRESERVE,
        declared_type_coercions=(("varchar(150)", "string"),),
        blank_key_behavior=BlankKeyBehaviorV1.NEVER_MATCH,
        nulls_never_match=True,
        composite_key_ordering=CompositeKeyOrderingV1.DECLARED_PAIR_ORDER,
    )
    base.update(over)
    return JoinKeyNormalizationPolicy(**base)


def _binding(**over) -> PhysicalTemporalJoinBindingV1:
    base = dict(
        dataset_temporal_policy_revision_id="dtp_" + "d" * 64,
        effective_from_column_ref=None,
        effective_to_column_ref=None,
        availability_or_knowledge_time_column_ref=f"{CUST}.business_dt",
        cutoff_parameter_ref="report_cutoff",
        source_binding_revision_id="sbr_cust_1",
        tie_break_column_refs=(f"{CUST}.business_dt",),
    )
    base.update(over)
    return PhysicalTemporalJoinBindingV1(**base)


def _policy(**over) -> JoinValidationPolicyRevisionV1:
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
        declared_at="2026-08-24T00:00:00Z",
    )
    base.update(over)
    return JoinValidationPolicyRevisionV1(**base)


def _segment(**over) -> PhysicalJoinSegmentV1:
    base = dict(
        realization_revision_id="bjr_1",
        column_pairs=(ColumnPairV1(f"{CUST}.cust_num", f"{TXN}.cif_id"),),
        predicates=(FixedValueReferencePredicateV1(
            predicate_id="pred_1",
            logical_column_ref=f"{CUST}.business_dt",
            value_ref="report_cutoff"),),
        directional_cardinality=DirectionalCardinalityVerdictV1.unknown(),
        realization_content_hash="e" * 64,
        realization_dependency_hash="f" * 64,
        key_normalization=_normalization(),
        temporal_binding=_binding(),
    )
    base.update(over)
    return PhysicalJoinSegmentV1(**base)


def _physical(context_id: str, **over) -> PhysicalExecutionPlanV1:
    base = dict(
        logical_digest_ref=logical_digest(_logical()),
        execution_context_revision_id=context_id,
        source_binding_revisions=((CUST, "sbr_cust_1"), (TXN, "sbr_txn_1")),
        segments=(_segment(),),
        join_validation_policy_revision_id=_policy().revision_id,
    )
    base.update(over)
    return PhysicalExecutionPlanV1(**base)


def _render_profile(**over) -> RenderProfileV1:
    base = dict(
        engine="pyspark_kedro",
        compiler_version="compiler-1.0.0",
        renderer_version="renderer-1.0.0",
        template_versions=(("node", "1.0.0"), ("pipeline", "2.0.0")),
    )
    base.update(over)
    return RenderProfileV1(**base)


def _genconfig(**over) -> GenerationConfigurationV1:
    base = dict(
        population_spine_ref=CUST,
        target_mode="sandbox_preview",
        target_ref="preview_workspace",
        cadence="on_demand",
        physical_type_policy="engine_default",
        policy_realization_revision_ids=("prr_direction_1",),
        engine_settings=(("shuffle_partitions", 16),),
    )
    base.update(over)
    return GenerationConfigurationV1(**base)


def _output_contract(**over) -> MemberOutputContractV1:
    base = dict(
        output_feature_name="outgoing_increase_30d",
        output_column_name="outgoing_increase_30d",
        empty_window_value=0,
        not_applicable_value=None,
        null_input_behavior=NullInputBehaviorV1.PROPAGATE_NULL,
        physical_type="decimal(38,2)",
        decimal_scale=2,
        rounding_policy=RoundingPolicyV1.HALF_EVEN,
        overflow_policy=OverflowPolicyV1.REFUSE,
    )
    base.update(over)
    return MemberOutputContractV1(**base)


def _context(db, environment_id="env-uat", tier=ExecutionTier.SANDBOX) -> str:
    return ensure_execution_context_revision(
        db, environment_id=environment_id, execution_tier=tier,
        purpose=FEATURE_GENERATION_PURPOSE)


def _count(db, table: str) -> int:
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ── the migration applies ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("table", TABLES)
def test_the_1134_tables_exist(db, table) -> None:
    assert db.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is not None


# ── round-trip fidelity, layer by layer ───────────────────────────────────────────────────────
def test_logical_plan_round_trip_is_the_same_contract(db) -> None:
    plan = _logical()
    record = ensure_logical_feature_plan(db, plan=plan)
    assert record.logical_digest == logical_digest(plan)
    assert record.revision_id == LOGICAL_PLAN_ID_PREFIX + record.logical_digest
    assert record.provenance_id is not None
    assert record.provenance_id.startswith(LOGICAL_PROVENANCE_ID_PREFIX)

    loaded = load_logical_feature_plan(db, record.revision_id,
                                      provenance_id=record.provenance_id)
    assert loaded == plan
    assert loaded.content_payload() == plan.content_payload()
    assert logical_digest(loaded) == record.logical_digest


def test_physical_plan_round_trip_is_the_same_contract(db) -> None:
    ensure_logical_feature_plan(db, plan=_logical())
    plan = _physical(_context(db))
    revision_id = ensure_physical_execution_plan(db, plan=plan)
    assert revision_id == PHYSICAL_PLAN_ID_PREFIX + physical_digest(plan)

    loaded = load_physical_execution_plan(db, revision_id)
    assert loaded == plan
    assert loaded.content_payload() == plan.content_payload()
    assert physical_digest(loaded) == physical_digest(plan)


def test_render_profile_round_trip_is_the_same_contract(db) -> None:
    profile = _render_profile()
    revision_id = ensure_render_profile(db, profile=profile)
    assert revision_id == RENDER_PROFILE_ID_PREFIX + render_digest(profile)
    loaded = load_render_profile(db, revision_id)
    assert loaded == profile
    assert render_digest(loaded) == render_digest(profile)


def test_generation_configuration_round_trip_is_the_same_contract(db) -> None:
    configuration = _genconfig()
    revision_id = ensure_generation_configuration(db, configuration=configuration)
    assert revision_id == (
        GENERATION_CONFIGURATION_ID_PREFIX + generation_configuration_digest(configuration))
    loaded = load_generation_configuration(db, revision_id)
    assert loaded == configuration
    assert generation_configuration_digest(loaded) == generation_configuration_digest(
        configuration)


def test_member_output_contract_round_trip_is_the_same_contract(db) -> None:
    contract = _output_contract()
    revision_id = ensure_member_output_contract(db, contract=contract)
    assert revision_id.startswith(MEMBER_OUTPUT_CONTRACT_ID_PREFIX)
    loaded = load_member_output_contract(db, revision_id)
    assert loaded == contract
    assert loaded.content_payload() == contract.content_payload()


def test_loads_of_missing_ids_return_none(db) -> None:
    assert load_logical_feature_plan(db, LOGICAL_PLAN_ID_PREFIX + "0" * 64) is None
    assert load_physical_execution_plan(db, PHYSICAL_PLAN_ID_PREFIX + "0" * 64) is None
    assert load_render_profile(db, RENDER_PROFILE_ID_PREFIX + "0" * 64) is None
    assert load_generation_configuration(db, GENERATION_CONFIGURATION_ID_PREFIX + "0" * 64) is None
    assert load_member_output_contract(db, MEMBER_OUTPUT_CONTRACT_ID_PREFIX + "0" * 64) is None
    assert load_identity_digest_record(db, IDENTITY_DIGEST_ID_PREFIX + "0" * 64) is None


def test_the_stored_bytes_are_the_canonical_serialization(db) -> None:
    """The row's ``content`` IS the contract's canonical payload — the thing the digest was taken
    over, not a re-spelling of it."""
    plan = _logical()
    record = ensure_logical_feature_plan(db, plan=plan)
    stored = db.execute(
        "SELECT content FROM logical_feature_plan_revision WHERE revision_id = %s",
        (record.revision_id,)).fetchone()[0]
    assert stored == plan.content_payload()


def test_a_scrambled_declaration_order_persists_as_the_canonical_representative(db) -> None:
    """Order-insensitive fields (operand bindings, selected parameters, policy identities) are
    SORTED by the contract's own canonicalization, so a caller's declaration order does not fork
    identity — and the loaded value is the canonical representative of that identity."""
    plan = _logical()
    scrambled = _logical(
        operand_bindings=tuple(reversed(plan.operand_bindings)),
        selected_parameters=tuple(reversed(plan.selected_parameters)))
    assert logical_digest(scrambled) == logical_digest(plan)
    first = ensure_logical_feature_plan(db, plan=plan)
    second = ensure_logical_feature_plan(db, plan=scrambled)
    assert first.revision_id == second.revision_id
    loaded = load_logical_feature_plan(db, second.revision_id)
    assert loaded is not None
    assert loaded.content_payload() == scrambled.content_payload() == plan.content_payload()
    assert loaded.operand_bindings == plan.operand_bindings


# ── content-addressed idempotency ─────────────────────────────────────────────────────────────
def test_same_content_same_id_one_row_for_every_layer(db) -> None:
    ensure_logical_feature_plan(db, plan=_logical())
    context = _context(db)
    calls = (
        (lambda: ensure_logical_feature_plan(db, plan=_logical()).revision_id,
         "logical_feature_plan_revision"),
        (lambda: ensure_physical_execution_plan(db, plan=_physical(context)),
         "physical_execution_plan_revision"),
        (lambda: ensure_render_profile(db, profile=_render_profile()),
         "render_profile_revision"),
        (lambda: ensure_generation_configuration(db, configuration=_genconfig()),
         "generation_configuration_revision"),
        (lambda: ensure_member_output_contract(db, contract=_output_contract()),
         "member_output_contract_revision"),
    )
    for call, table in calls:
        ids = {call() for _ in range(3)}
        assert len(ids) == 1, table
        assert _count(db, table) == 1, table


def test_different_content_mints_different_revisions(db) -> None:
    ensure_logical_feature_plan(db, plan=_logical())
    other = _logical(operation="mean_window_delta")
    ensure_logical_feature_plan(db, plan=other)
    assert _count(db, "logical_feature_plan_revision") == 2


# ── R9's provenance side-car: recorded, never identity ────────────────────────────────────────
def test_two_hypotheses_reaching_one_feature_are_one_plan_and_two_provenance_records(db) -> None:
    """R9's staleness law at the persistence layer: the SAME meaning reached from a different
    hypothesis is the SAME feature — one plan row — and both hypotheses are kept."""
    first = ensure_logical_feature_plan(db, plan=_logical())
    second = ensure_logical_feature_plan(db, plan=_logical(
        provenance=_provenance(hypothesis_text="Which new customers are ramping up spend?")))
    assert first.revision_id == second.revision_id
    assert first.provenance_id != second.provenance_id
    assert _count(db, "logical_feature_plan_revision") == 1
    assert set(logical_plan_provenance_ids(db, first.revision_id)) == {
        first.provenance_id, second.provenance_id}
    kept = load_logical_plan_provenance(db, second.provenance_id)
    assert kept is not None
    assert kept.hypothesis_text == "Which new customers are ramping up spend?"


def test_loading_without_a_provenance_id_yields_the_empty_side_car(db) -> None:
    """Honest absence: a load that names no provenance gets the EMPTY side-car, never some other
    caller's hypothesis attached to a plan that never carried it."""
    record = ensure_logical_feature_plan(db, plan=_logical())
    loaded = load_logical_feature_plan(db, record.revision_id)
    assert loaded is not None
    assert loaded.provenance == LogicalPlanProvenanceV1()
    assert logical_digest(loaded) == record.logical_digest


def test_an_empty_provenance_side_car_records_nothing(db) -> None:
    record = ensure_logical_feature_plan(
        db, plan=_logical(provenance=LogicalPlanProvenanceV1()))
    assert record.provenance_id is None
    assert logical_plan_provenance_ids(db, record.revision_id) == ()


def test_provenance_of_a_missing_id_is_none(db) -> None:
    assert load_logical_plan_provenance(db, LOGICAL_PROVENANCE_ID_PREFIX + "0" * 64) is None


# ── R2/R9: the layers are structurally independent ────────────────────────────────────────────
def test_a_second_physical_plan_leaves_the_logical_row_untouched(db) -> None:
    """The task's requirement 3, pinned: persisting physical plans against one logical plan may
    never move the logical row, its digest, its bytes or its recorded_at."""
    plan = _logical()
    record = ensure_logical_feature_plan(db, plan=plan)
    before = db.execute(
        "SELECT logical_digest, content, content_hash, recorded_at "
        "FROM logical_feature_plan_revision WHERE revision_id = %s",
        (record.revision_id,)).fetchone()

    sandbox = ensure_physical_execution_plan(db, plan=_physical(_context(db)))
    production = ensure_physical_execution_plan(db, plan=_physical(
        _context(db, environment_id="env-prod", tier=ExecutionTier.PRODUCTION)))
    assert sandbox != production

    after = db.execute(
        "SELECT logical_digest, content, content_hash, recorded_at "
        "FROM logical_feature_plan_revision WHERE revision_id = %s",
        (record.revision_id,)).fetchone()
    assert after == before
    assert _count(db, "logical_feature_plan_revision") == 1
    assert _count(db, "physical_execution_plan_revision") == 2
    assert logical_digest(plan) == record.logical_digest
    # ...and both physical rows pin the SAME logical identity.
    rows = db.execute(
        "SELECT DISTINCT logical_digest FROM physical_execution_plan_revision").fetchall()
    assert [row[0] for row in rows] == [record.logical_digest]


def test_the_logical_row_carries_no_execution_material(db) -> None:
    record = ensure_logical_feature_plan(db, plan=_logical())
    ensure_physical_execution_plan(db, plan=_physical(_context(db)))
    content = db.execute(
        "SELECT content::text FROM logical_feature_plan_revision WHERE revision_id = %s",
        (record.revision_id,)).fetchone()[0]
    for token in ("execution_context", "ecx_", "jvp_", "sbr_", "dtp_"):
        assert token not in content


# ── immutability ──────────────────────────────────────────────────────────────────────────────
def _seed_every_table(db) -> None:
    record = ensure_logical_feature_plan(db, plan=_logical())
    ensure_physical_execution_plan(db, plan=_physical(_context(db)))
    ensure_render_profile(db, profile=_render_profile())
    ensure_generation_configuration(db, configuration=_genconfig())
    ensure_member_output_contract(db, contract=_output_contract())
    record_identity_digest(db, stage=STAGE_FORMULA_BINDING, inputs={
        "logical_digest": record.logical_digest,
        "formula_content_hash": "9" * 64,
        "formula_method_identity": "llm_authoring_v1",
    })


@pytest.mark.parametrize("table", TABLES)
def test_update_and_delete_refuse(db, table) -> None:
    _seed_every_table(db)
    for statement in (f"UPDATE {table} SET recorded_at = now()", f"DELETE FROM {table}"):
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            with db.transaction():
                db.execute(statement)
        assert "append-only" in str(excinfo.value)
        assert table in str(excinfo.value)


@pytest.mark.parametrize("table", TABLES)
def test_truncate_refuses(db, table) -> None:
    """A4's discovery, honored: no FK points at any of these append-only tables, so the TRUNCATE
    raiser — not a foreign-key error — is what refuses."""
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        with db.transaction():
            db.execute(f"TRUNCATE {table}")
    assert "append-only" in str(excinfo.value)


# ── the digest columns are the join points, and cannot drift from their content ───────────────
@pytest.mark.parametrize("table,prefix,digest_column,extra", [
    ("logical_feature_plan_revision", LOGICAL_PLAN_ID_PREFIX, "logical_digest", {}),
    ("physical_execution_plan_revision", PHYSICAL_PLAN_ID_PREFIX, "physical_digest",
     {"logical_digest": "8" * 64, "execution_context_revision_id": "ecx_" + "8" * 64,
      "join_validation_policy_revision_id": "jvp_" + "8" * 64}),
    ("render_profile_revision", RENDER_PROFILE_ID_PREFIX, "render_profile_digest", {}),
    ("generation_configuration_revision", GENERATION_CONFIGURATION_ID_PREFIX,
     "generation_configuration_digest", {}),
])
def test_the_digest_column_must_equal_the_content_hash(
        db, table, prefix, digest_column, extra) -> None:
    """The structural half of the guard: the join point is the content hash BY CONSTRUCTION —
    a row whose published digest is a second, independent value cannot be written at all."""
    columns = ["revision_id", digest_column, "content", "content_hash", *extra]
    values = [prefix + "1" * 64, "2" * 64, Jsonb({"contract": "x"}), "1" * 64, *extra.values()]
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join(['%s'] * len(values))})",
                tuple(values))


@pytest.mark.parametrize("table,prefix", [
    ("logical_feature_plan_revision", LOGICAL_PLAN_ID_PREFIX),
    ("render_profile_revision", RENDER_PROFILE_ID_PREFIX),
    ("member_output_contract_revision", MEMBER_OUTPUT_CONTRACT_ID_PREFIX),
])
def test_the_revision_id_must_derive_from_the_content_hash(db, table, prefix) -> None:
    columns = "revision_id, content, content_hash"
    values = (prefix + "3" * 64, Jsonb({"contract": "x"}), "4" * 64)
    if table != "member_output_contract_revision":
        digest_column = ("logical_digest" if table == "logical_feature_plan_revision"
                         else "render_profile_digest")
        columns = f"revision_id, {digest_column}, content, content_hash"
        values = (prefix + "3" * 64, "4" * 64, Jsonb({"contract": "x"}), "4" * 64)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({', '.join(['%s'] * len(values))})",
                values)


def test_a_tampered_logical_row_refuses_to_load(db) -> None:
    """The behavioural half: a row whose stored CONTENT no longer produces its stored digest is
    corruption and is never served (the defect this whole chain exists to prevent)."""
    tampered = _logical(operation="mean_window_delta").content_payload()
    forged = "5" * 64
    db.execute(
        "INSERT INTO logical_feature_plan_revision "
        "  (revision_id, logical_digest, content, content_hash) VALUES (%s, %s, %s, %s)",
        (LOGICAL_PLAN_ID_PREFIX + forged, forged, Jsonb(tampered), forged))
    with pytest.raises(IdentityStoreConflict) as excinfo:
        load_logical_feature_plan(db, LOGICAL_PLAN_ID_PREFIX + forged)
    assert "digest" in str(excinfo.value)


def test_a_tampered_render_profile_row_refuses_to_load(db) -> None:
    tampered = _render_profile(renderer_version="renderer-9.9.9").content_payload()
    forged = "6" * 64
    db.execute(
        "INSERT INTO render_profile_revision "
        "  (revision_id, render_profile_digest, content, content_hash) VALUES (%s, %s, %s, %s)",
        (RENDER_PROFILE_ID_PREFIX + forged, forged, Jsonb(tampered), forged))
    with pytest.raises(IdentityStoreConflict):
        load_render_profile(db, RENDER_PROFILE_ID_PREFIX + forged)


def test_a_tampered_member_output_contract_refuses_to_load(db) -> None:
    tampered = _output_contract(empty_window_value=None).content_payload()
    forged = "7" * 64
    db.execute(
        "INSERT INTO member_output_contract_revision (revision_id, content, content_hash) "
        "VALUES (%s, %s, %s)",
        (MEMBER_OUTPUT_CONTRACT_ID_PREFIX + forged, Jsonb(tampered), forged))
    with pytest.raises(IdentityStoreConflict):
        load_member_output_contract(db, MEMBER_OUTPUT_CONTRACT_ID_PREFIX + forged)


def test_content_hash_uniqueness_survives_a_hostile_double_insert(db) -> None:
    record = ensure_logical_feature_plan(db, plan=_logical())
    content = db.execute(
        "SELECT content FROM logical_feature_plan_revision WHERE revision_id = %s",
        (record.revision_id,)).fetchone()[0]
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO logical_feature_plan_revision "
                "  (revision_id, logical_digest, content, content_hash) VALUES (%s, %s, %s, %s)",
                (LOGICAL_PLAN_ID_PREFIX + record.logical_digest, record.logical_digest,
                 Jsonb(content), record.logical_digest))
    assert _count(db, "logical_feature_plan_revision") == 1


# ── store validation instead of foreign keys (A4's discovery) ─────────────────────────────────
def test_a_physical_plan_pinning_an_unpersisted_logical_plan_refuses(db) -> None:
    with pytest.raises(IdentityPersistenceDefect) as excinfo:
        ensure_physical_execution_plan(db, plan=_physical(_context(db)))
    assert "logical" in str(excinfo.value)
    assert _count(db, "physical_execution_plan_revision") == 0


def test_a_physical_plan_pinning_an_unpersisted_context_refuses(db) -> None:
    ensure_logical_feature_plan(db, plan=_logical())
    with pytest.raises(IdentityPersistenceDefect) as excinfo:
        ensure_physical_execution_plan(
            db, plan=_physical("ecx_" + "0" * 64))
    assert "execution context" in str(excinfo.value)
    assert _count(db, "physical_execution_plan_revision") == 0


# ── typed refusals BEFORE any SQL ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("call", [
    lambda conn: ensure_logical_feature_plan(conn, plan={"contract": "logical_feature_plan_v2"}),
    lambda conn: ensure_physical_execution_plan(conn, plan=None),
    lambda conn: ensure_render_profile(conn, profile="renderer-1.0.0"),
    lambda conn: ensure_generation_configuration(conn, configuration=42),
    lambda conn: ensure_member_output_contract(conn, contract=_render_profile()),
    lambda conn: record_identity_digest(conn, stage="compile", inputs={}),
    lambda conn: record_identity_digest(conn, stage=STAGE_FORMULA_BINDING, inputs={
        "logical_digest": "8" * 64, "formula_content_hash": "9" * 64}),
    lambda conn: record_identity_digest(conn, stage=STAGE_FORMULA_BINDING, inputs={
        "logical_digest": "8" * 64, "formula_content_hash": "9" * 64,
        "formula_method_identity": "m", "extra": "smuggled"}),
    lambda conn: record_identity_digest(conn, stage=STAGE_FORMULA_BINDING, inputs="everything"),
])
def test_wrong_types_and_shapes_refuse_before_any_sql(call) -> None:
    with pytest.raises(IdentityPersistenceDefect):
        call(_NeverTouched())


# ── the digest chain ──────────────────────────────────────────────────────────────────────────
def _chain(db) -> dict[str, object]:
    """Record all five composed stages in order and hand back everything the assertions need."""
    plan = _logical()
    record = ensure_logical_feature_plan(db, plan=plan)
    physical = _physical(_context(db))
    ensure_physical_execution_plan(db, plan=physical)
    contract = _output_contract()
    ensure_member_output_contract(db, contract=contract)
    profile = _render_profile()
    ensure_render_profile(db, profile=profile)
    configuration = _genconfig()
    ensure_generation_configuration(db, configuration=configuration)

    binding = record_identity_digest(db, stage=STAGE_FORMULA_BINDING, inputs={
        "logical_digest": record.logical_digest,
        "formula_content_hash": "9" * 64,
        "formula_method_identity": "llm_authoring_v1",
    })
    execution_input = record_identity_digest(db, stage=STAGE_MEMBER_EXECUTION_INPUT, inputs={
        "formula_binding_digest": binding.digest,
        "physical_digest": physical_digest(physical),
        "member_output_contract": contract.content_payload(),
    })
    compile_record = record_identity_digest(db, stage=STAGE_MEMBER_COMPILE, inputs={
        "member_execution_input_digest": execution_input.digest,
        "ir_hash": "ir_hash_1",
        "policy_occurrence_bindings": [["occurrence_1", "prr_direction_1"]],
    })
    build = record_identity_digest(db, stage=STAGE_BUILD_COMPILATION, inputs={
        "target_and_spine_revision": "tsr_1",
        "ordered_member_compile_digests": [compile_record.digest],
        "generation_configuration_digest": generation_configuration_digest(configuration),
    })
    sealed = record_identity_digest(db, stage=STAGE_SEALED_ARTIFACT, inputs={
        "build_compilation_digest": build.digest,
        "render_profile_digest": render_digest(profile),
        "project_digest": "project_digest_bytes_1",
    })
    return {
        "plan": plan, "record": record, "physical": physical, "contract": contract,
        "profile": profile, "configuration": configuration, "binding": binding,
        "execution_input": execution_input, "compile": compile_record, "build": build,
        "sealed": sealed,
    }


def test_every_recorded_stage_equals_step_threes_own_function(db) -> None:
    c = _chain(db)
    assert c["binding"].digest == formula_binding_digest(
        c["record"].logical_digest, "9" * 64, "llm_authoring_v1")
    assert c["execution_input"].digest == member_execution_input_digest(
        c["binding"].digest, physical_digest(c["physical"]), c["contract"])
    assert c["compile"].digest == member_compile_digest(
        c["execution_input"].digest, "ir_hash_1", (("occurrence_1", "prr_direction_1"),))
    assert c["build"].digest == build_compilation_digest(
        "tsr_1", (c["compile"].digest,),
        generation_configuration_digest(c["configuration"]))
    assert c["sealed"].digest == sealed_artifact_identity(
        c["build"].digest, render_digest(c["profile"]), "project_digest_bytes_1")


def test_a_recorded_stage_round_trips_and_resolves_by_its_digest(db) -> None:
    c = _chain(db)
    for stage_record in (c["binding"], c["execution_input"], c["compile"], c["build"],
                         c["sealed"]):
        loaded = load_identity_digest_record(db, stage_record.digest_id)
        assert loaded == stage_record
        assert loaded.digest_id == IDENTITY_DIGEST_ID_PREFIX + loaded.content_hash
        resolved = resolve_identity_digest(db, stage=loaded.stage, digest=loaded.digest)
        assert resolved == stage_record
    assert _count(db, "identity_digest_record") == 5


def test_recording_the_same_stage_twice_is_one_row(db) -> None:
    record = ensure_logical_feature_plan(db, plan=_logical())
    inputs = {"logical_digest": record.logical_digest, "formula_content_hash": "9" * 64,
              "formula_method_identity": "llm_authoring_v1"}
    ids = {record_identity_digest(db, stage=STAGE_FORMULA_BINDING, inputs=inputs).digest_id
           for _ in range(3)}
    assert len(ids) == 1
    assert _count(db, "identity_digest_record") == 1


def test_the_stage_vocabulary_is_closed_in_the_database_too(db) -> None:
    assert IDENTITY_DIGEST_STAGES == (
        STAGE_FORMULA_BINDING, STAGE_MEMBER_EXECUTION_INPUT, STAGE_MEMBER_COMPILE,
        STAGE_BUILD_COMPILATION, STAGE_SEALED_ARTIFACT)
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO identity_digest_record (digest_id, stage, digest, inputs, "
                "content_hash) VALUES (%s, 'compilation', %s, %s, %s)",
                (IDENTITY_DIGEST_ID_PREFIX + "a" * 64, "b" * 64, Jsonb({}), "a" * 64))


@pytest.mark.parametrize("stage,missing_input", [
    (STAGE_FORMULA_BINDING, "logical_digest"),
    (STAGE_MEMBER_EXECUTION_INPUT, "physical_digest"),
    (STAGE_MEMBER_EXECUTION_INPUT, "formula_binding_digest"),
    (STAGE_MEMBER_EXECUTION_INPUT, "member_output_contract"),
    (STAGE_MEMBER_COMPILE, "member_execution_input_digest"),
    (STAGE_BUILD_COMPILATION, "ordered_member_compile_digests"),
    (STAGE_BUILD_COMPILATION, "generation_configuration_digest"),
    (STAGE_SEALED_ARTIFACT, "build_compilation_digest"),
    (STAGE_SEALED_ARTIFACT, "render_profile_digest"),
])
def test_a_stage_whose_predecessor_was_never_persisted_refuses(db, stage, missing_input) -> None:
    """A digest record is a link in a CHAIN: every input this store owns must already exist, or
    the record would pin an identity nobody can resolve. No FK expresses this (the tables are
    append-only); the store's own read does."""
    c = _chain(db)
    absent = "0" * 64
    inputs = {
        STAGE_FORMULA_BINDING: {
            "logical_digest": c["record"].logical_digest,
            "formula_content_hash": "9" * 64,
            "formula_method_identity": "llm_authoring_v1"},
        STAGE_MEMBER_EXECUTION_INPUT: {
            "formula_binding_digest": c["binding"].digest,
            "physical_digest": physical_digest(c["physical"]),
            "member_output_contract": c["contract"].content_payload()},
        STAGE_MEMBER_COMPILE: {
            "member_execution_input_digest": c["execution_input"].digest,
            "ir_hash": "ir_hash_2",
            "policy_occurrence_bindings": [["occurrence_1", "prr_direction_1"]]},
        STAGE_BUILD_COMPILATION: {
            "target_and_spine_revision": "tsr_2",
            "ordered_member_compile_digests": [c["compile"].digest],
            "generation_configuration_digest": generation_configuration_digest(
                c["configuration"])},
        STAGE_SEALED_ARTIFACT: {
            "build_compilation_digest": c["build"].digest,
            "render_profile_digest": render_digest(c["profile"]),
            "project_digest": "project_digest_bytes_2"},
    }[stage]
    if missing_input == "member_output_contract":
        inputs[missing_input] = _output_contract(
            output_feature_name="never_persisted").content_payload()
    elif missing_input == "ordered_member_compile_digests":
        inputs[missing_input] = [absent]
    else:
        inputs[missing_input] = absent
    with pytest.raises(IdentityPersistenceDefect) as excinfo:
        record_identity_digest(db, stage=stage, inputs=inputs)
    assert missing_input.rstrip("s") in str(excinfo.value) or "persisted" in str(excinfo.value)


def test_a_tampered_digest_record_refuses_to_load(db) -> None:
    """The guard that makes the stored digest un-driftable: the load RECOMPUTES the stage from
    the stored inputs and refuses when the two disagree."""
    c = _chain(db)
    honest = load_identity_digest_record(db, c["binding"].digest_id)
    forged = "5" * 64
    db.execute(
        "INSERT INTO identity_digest_record (digest_id, stage, digest, inputs, content_hash) "
        "VALUES (%s, %s, %s, %s, %s)",
        (IDENTITY_DIGEST_ID_PREFIX + forged, honest.stage, "e" * 64, Jsonb(honest.inputs),
         forged))
    with pytest.raises(IdentityStoreConflict) as excinfo:
        load_identity_digest_record(db, IDENTITY_DIGEST_ID_PREFIX + forged)
    assert "digest" in str(excinfo.value)


def test_one_digest_resolves_to_exactly_one_input_record(db) -> None:
    """UNIQUE(stage, digest): the chain is walkable BACKWARDS — a digest can never name two
    different input sets."""
    c = _chain(db)
    honest = load_identity_digest_record(db, c["binding"].digest_id)
    other = dict(honest.inputs)
    other["formula_method_identity"] = "deterministic_compiler_v1"
    forged = "d" * 64
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO identity_digest_record (digest_id, stage, digest, inputs, "
                "content_hash) VALUES (%s, %s, %s, %s, %s)",
                (IDENTITY_DIGEST_ID_PREFIX + forged, honest.stage, honest.digest, Jsonb(other),
                 forged))


def test_resolve_of_an_unknown_digest_is_none(db) -> None:
    assert resolve_identity_digest(
        db, stage=STAGE_FORMULA_BINDING, digest="0" * 64) is None


def test_member_order_inside_a_build_is_identity(db) -> None:
    """The two-member order-reversal pin, at the persistence layer: reversing the ordered member
    compiles is a DIFFERENT build record, not the same one."""
    c = _chain(db)
    second = record_identity_digest(db, stage=STAGE_MEMBER_COMPILE, inputs={
        "member_execution_input_digest": c["execution_input"].digest,
        "ir_hash": "ir_hash_2",
        "policy_occurrence_bindings": [["occurrence_1", "prr_direction_1"]],
    })
    ordered = record_identity_digest(db, stage=STAGE_BUILD_COMPILATION, inputs={
        "target_and_spine_revision": "tsr_1",
        "ordered_member_compile_digests": [c["compile"].digest, second.digest],
        "generation_configuration_digest": generation_configuration_digest(c["configuration"]),
    })
    reversed_build = record_identity_digest(db, stage=STAGE_BUILD_COMPILATION, inputs={
        "target_and_spine_revision": "tsr_1",
        "ordered_member_compile_digests": [second.digest, c["compile"].digest],
        "generation_configuration_digest": generation_configuration_digest(c["configuration"]),
    })
    assert ordered.digest != reversed_build.digest
    assert ordered.digest_id != reversed_build.digest_id
