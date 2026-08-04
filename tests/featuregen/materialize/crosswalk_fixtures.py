"""The Task-11 crosswalk fixture pair, seeded for COMPILATION (Release C Task 12).

ONE bank for every Task-12 suite — planning, Gate 2, rendering and acceptance — for the reason
``_crosswalk_fixtures`` states about its own: three hand-rolled copies of a load-bearing fixture is
how two suites end up asserting against two different banks and both passing.

**It is the same bank, deliberately.** The crosswalk DEFINITION, its endpoints and its mapping pairs
come from ``tests.featuregen.overlay.upload._crosswalk_fixtures`` unchanged — an internal account
number (``cib::public.bo_cib_account.acct_no``, namespace ``internal_account``) related to a
correspondent's account reference (``ftr::public.tran_repos.counter_party_acct_no``, namespace
``external_account``) THROUGH ``cib::public.acct_xref``. Two real schemes across two catalogs, which
is the shape that actually needs a crosswalk: were both sides the same concept in one namespace it
would be a direct-equality bridge and the bridge family would own it.

What this module ADDS is everything a compilation needs and a governance fixture has no reason to
carry: a governed availability fact and an event-time column on the source relation, a declared
cluster inventory for all three tables, and the composed measurement + admission decision an
``AdmittedCrosswalkV1`` is assembled from.

**The measured asymmetry is the point.** :func:`admitted` measures the composition 1:1 FORWARD and
fanning in REVERSE (``target_to_source_max_matches=2``), which is the ordinary shape of a real
mapping table and is what lets one fixture serve both the "plans" and the "refuses independently"
tests without either of them being a special case built to pass.

FIXTURES ONLY. No engine, no LLM, no cluster.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from tests.featuregen.materialize import fixtures
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB_TABLE,
    FTR_TABLE,
    MAP_TABLE,
    binding,
    definition,
    leg,
    mapping_observation,
)

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.formula.canonical import formula_content_hash
from featuregen.formula.schema import (
    CANONICALIZATION_VERSION,
    FORMULA_SCHEMA_VERSION,
    OPERATION_GRAMMAR_VERSION,
    OUTPUT_POLICY_VERSION,
    AdditivityClass,
    AggregateExpression,
    AggregateFunction,
    DecimalPolicy,
    EmptyWindowResult,
    FormulaOutputPolicyV1,
    Grain,
    Inclusivity,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    SourceRelation,
    TypedFormulaV1,
    UnaryBody,
    WindowBasis,
    WindowPolicy,
    WindowUnit,
)
from featuregen.materialize.admission import AdmittedFeature
from featuregen.materialize.codes import MaterializationRefused
from featuregen.materialize.compile.chain import NodeAssemblyInputs
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
    ContractGroup,
    derive_group_contract,
)
from featuregen.materialize.group_plan import PlannedFeature, build_group_plan
from featuregen.materialize.identity import SealedProject
from featuregen.materialize.inputs import derive_requirement
from featuregen.materialize.inventory import (
    ClusterInventoryV1,
    TableLayout,
    VerifiedUnpartitioned,
)
from featuregen.materialize.ir import authorize_compilation, compile_ir, ir_hash
from featuregen.materialize.physical_types import PhysicalType, resolve_physical_type
from featuregen.materialize.render.project import project_datasets, render_project
from featuregen.materialize.spine import (
    CurrentSnapshot,
    PopulationSemantics,
    SpineSourceDeclarationV1,
)
from featuregen.overlay.identity import fact_key as _fact_key
from featuregen.overlay.upload.bridge_realization import (
    ExecutionTier,
    RealizationApplicabilityScopeV1,
)
from featuregen.overlay.upload.crosswalk import JoinLegKind, JoinLegPinV1
from featuregen.overlay.upload.crosswalk_admission import (
    AdmittedCrosswalkV1,
    CrosswalkAdmissionPolicyV1,
    admitted_crosswalk_execution,
    evaluate_crosswalk_admission,
)
from featuregen.overlay.upload.crosswalk_observation import (
    MAPPING_TO_TARGET,
    SOURCE_TO_MAPPING,
    compose_crosswalk_observation,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.temporal_policy import DatasetRowSelectionV1, TemporalSelectionKind
from featuregen.overlay.upload.upload_catalog import table_ref as _catalog_table_ref

NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
TZ = "Asia/Kolkata"

#: The population's entity. The FTR side is the correspondent's own account scheme, so a row of the
#: published table is one correspondent account — reachable from the CIB rows the feature is
#: computed over ONLY through the mapping table.
ENTITY = "counterparty_account"

#: The physical schemas the three tables resolve onto. Deliberately NOT ``public``: refs are
#: schema-flattened (§3.5) and the resolved schema is what the generated project reads.
CIB_SCHEMA, FTR_SCHEMA = "dpl_eib", "dpl_ftr"
ENVIRONMENT = "edp_cluster"

CIB = normalize_ref("cib", "public", CIB_TABLE)
FTR = normalize_ref("ftr", "public", FTR_TABLE)
MAP = normalize_ref("cib", "public", MAP_TABLE)

CIB_ACCT = f"{CIB}.acct_no"
CIB_OPENED = f"{CIB}.opened_dt"
CIB_LOAD_TS = f"{CIB}.load_ts"
FTR_ACCT = f"{FTR}.counter_party_acct_no"
MAP_ACCT = f"{MAP}.acct_no"
MAP_EXT = f"{MAP}.ext_acct_ref"
MAP_VALID_FROM = f"{MAP}.valid_from"
MAP_VALID_TO = f"{MAP}.valid_to"

#: The bindings the measurement and the execution revision pin.
CIB_BINDING = binding("cib", CIB_TABLE, database=ENVIRONMENT, schema=CIB_SCHEMA)
FTR_BINDING = binding("ftr", FTR_TABLE, database=ENVIRONMENT, schema=FTR_SCHEMA)
MAP_BINDING = binding("cib", MAP_TABLE, database=ENVIRONMENT, schema=CIB_SCHEMA)

PRODUCTION_SCOPE = RealizationApplicabilityScopeV1(
    scope_id="crosswalk-production-scope", execution_tier=ExecutionTier.PRODUCTION,
    purposes=("feature_generation",), environment=ENVIRONMENT)
SANDBOX_SCOPE = RealizationApplicabilityScopeV1(
    scope_id="crosswalk-sandbox-scope", execution_tier=ExecutionTier.SANDBOX,
    purposes=("feature_generation",), environment=ENVIRONMENT)

#: The mapping table's governed temporal policy revision, as the execution revision pins it. The
#: id's SHAPE is load-bearing (``DatasetRowSelectionV1`` refuses anything that is not
#: ``dtp_<hash>``); its value is a fixture's.
TEMPORAL_POLICY = "dtp_" + "b" * 64
DATASET_PROFILE_HASH = "d" * 64
#: The run parameter the mapping row rule binds its cutoff to. Never a literal date — the contract
#: refuses one, because a literal would re-key the decision every day.
REPORT_CUTOFF_REF = "report_cutoff"

#: The published column's decimal policy. COUNT_ROWS publishes an integer, so this is carried only
#: because `TypedFormulaV1` requires one.
DECIMAL = DecimalPolicy(precision=38, scale=6, rounding=RoundingMode.HALF_EVEN,
                        overflow=OverflowBehavior.ERROR)


# ── the governed catalog ─────────────────────────────────────────────────────────────────────────

def column(db, source, table, name, *, schema, sensitivity=None, restriction=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name, sensitivity, effective_restriction) "
        "VALUES (%s, %s, 'column', %s, %s, %s, %s, %s)",
        (source, f"public.{table}.{name}", table, name, schema, sensitivity, restriction))


def table_node(db, source, table, *, schema):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, schema_name) "
        "VALUES (%s, %s, 'table', %s, %s)", (source, f"public.{table}", table, schema))


def watermark(db, source, *, head_seq=7):
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, now(), 'r', %s) ON CONFLICT (catalog_source) "
        "DO UPDATE SET last_completed_at = now(), head_seq = EXCLUDED.head_seq", (source, head_seq))


def govern_availability(db, source, table, column_name, *, event_id="ovf_evt_asof_cwx"):
    """Both halves of the shipped contract — the flat flag AND the fact row the link points at."""
    db.execute(
        "UPDATE graph_node SET is_as_of = true, availability_fact_event_id = %s "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (event_id, source, table, column_name))
    key = _fact_key(_catalog_table_ref(source, table), "availability_time")
    db.execute(
        "INSERT INTO overlay_fact_state (fact_key, catalog_source, object_ref, fact_type, status, "
        "value, confirmed_at, confirmed_event_id, updated_seq) "
        "VALUES (%s, %s, %s, 'availability_time', 'VERIFIED', %s, now(), %s, 1) "
        "ON CONFLICT (fact_key) DO UPDATE SET value = EXCLUDED.value, "
        "confirmed_event_id = EXCLUDED.confirmed_event_id",
        (key, source, f"public.{table}",
         json.dumps({"column": column_name, "basis": "ingested_at"}), event_id))


def govern_grain(db, source, table, column_name):
    db.execute(
        "UPDATE graph_node SET is_grain = true, grain_fact_event_id = 'ovf_evt_grain_cwx' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (source, table, column_name))


def govern_entity(db, source, table, column_name, *, entity=ENTITY):
    db.execute(
        "UPDATE graph_node SET entity = %s, entity_status = 'VERIFIED', "
        "entity_fact_key = 'sbf-entity-cwx', entity_fact_event_id = 'ovf_evt_entity_cwx' "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' AND column_name = %s",
        (entity, source, table, column_name))


def seed_catalog(db) -> None:
    """CIB (accounts + the cross-reference table) and FTR (the transaction repository).

    The mapping table carries BOTH validity columns, because the mapping row rule this task pins is
    a half-open ``[valid_from, valid_to)`` interval and a fixture without the second column could
    only ever exercise half of it.

    FTR is the POPULATION here — the correspondent's own account scheme — so its key carries the
    governed grain and entity facts the spine validator requires. That is what makes the crosswalk
    load-bearing rather than decorative: the feature is computed over CIB rows and published one
    row per FTR account, and the only route between the two is the mapping table.
    """
    for name in ("acct_no", "cust_num", "opened_dt", "load_ts"):
        column(db, "cib", CIB_TABLE, name, schema=CIB_SCHEMA)
    for name in ("acct_no", "ext_acct_ref", "valid_from", "valid_to"):
        column(db, "cib", MAP_TABLE, name, schema=CIB_SCHEMA)
    for name in ("counter_party_acct_no", "party_lei", "load_ts"):
        column(db, "ftr", FTR_TABLE, name, schema=FTR_SCHEMA)
    table_node(db, "cib", CIB_TABLE, schema=CIB_SCHEMA)
    table_node(db, "cib", MAP_TABLE, schema=CIB_SCHEMA)
    table_node(db, "ftr", FTR_TABLE, schema=FTR_SCHEMA)
    govern_availability(db, "cib", CIB_TABLE, "load_ts")
    govern_availability(db, "ftr", FTR_TABLE, "load_ts", event_id="ovf_evt_asof_ftr")
    govern_grain(db, "ftr", FTR_TABLE, "counter_party_acct_no")
    govern_entity(db, "ftr", FTR_TABLE, "counter_party_acct_no")
    watermark(db, "cib")
    watermark(db, "ftr")


# ── the declared inventory ───────────────────────────────────────────────────────────────────────

def _layout(schema, table, columns) -> TableLayout:
    return TableLayout(
        schema=schema, table=table, partition_columns=None,
        partition_mapping=VerifiedUnpartitioned(),
        columns=tuple((name, "string") for name in columns),
        location=f"hdfs://nn/warehouse/{schema}.db/{table}", rewritten_in_place=False)


INVENTORY = ClusterInventoryV1(
    environment_id=ENVIRONMENT,
    tables={
        f"{CIB_SCHEMA}.{CIB_TABLE}": _layout(
            CIB_SCHEMA, CIB_TABLE, ("acct_no", "cust_num", "opened_dt", "load_ts")),
        f"{CIB_SCHEMA}.{MAP_TABLE}": _layout(
            CIB_SCHEMA, MAP_TABLE, ("acct_no", "ext_acct_ref", "valid_from", "valid_to")),
        f"{FTR_SCHEMA}.{FTR_TABLE}": _layout(
            FTR_SCHEMA, FTR_TABLE, ("counter_party_acct_no", "party_lei", "load_ts")),
    },
    logical_schema_map={CIB: CIB_SCHEMA, MAP: CIB_SCHEMA, FTR: FTR_SCHEMA},
    engine_versions=fixtures.ENGINE_VERSIONS,
    captured_at="2026-08-04T00:00:00Z")


# ── the governed crosswalk ───────────────────────────────────────────────────────────────────────

DEFINITION = definition()
#: The definition CANONICALISES its two sides, so which endpoint is `source_endpoint` is decided by
#: content and not by the order the fixture typed them in. Every orientation-dependent value below
#: is derived from the definition rather than assumed — a fixture that assumed would silently swap
#: forward and reverse the day a ref changed, and every direction assertion with it.
SOURCE_REF = DEFINITION.source_endpoint.logical_table_ref
TARGET_REF = DEFINITION.target_endpoint.logical_table_ref
SOURCE_BINDING = CIB_BINDING if SOURCE_REF == CIB else FTR_BINDING
TARGET_BINDING = FTR_BINDING if SOURCE_REF == CIB else CIB_BINDING


def row_selection(**over) -> DatasetRowSelectionV1:
    """The mapping table's resolved row rule — Release B's answer, passed IN and never re-resolved.

    A half-open ``[valid_from, valid_to)`` interval bound to a run parameter: exactly what
    ``temporal_resolver._predicates_for`` emits for ``VALID_AT_REPORT_CUTOFF``, spelled here rather
    than resolved because the resolver needs a policy store and this fixture is about compilation.
    """
    kwargs = dict(
        dataset_logical_ref=MAP,
        dataset_profile_hash=DATASET_PROFILE_HASH,
        temporal_policy_revision_id=TEMPORAL_POLICY,
        selection_kind=TemporalSelectionKind.VALID_AT_REPORT_CUTOFF,
        cutoff_value_ref=REPORT_CUTOFF_REF,
        predicate_payloads=(
            {"kind": "effective_time", "column_ref": MAP_VALID_FROM, "operator": "<=",
             "parameter_ref": REPORT_CUTOFF_REF},
            {"kind": "effective_time", "column_ref": MAP_VALID_TO, "operator": ">",
             "parameter_ref": REPORT_CUTOFF_REF},
        ))
    kwargs.update(over)
    return DatasetRowSelectionV1(**kwargs)


def observation(**over):
    """The composed measurement: 1:1 forward, FANNING in reverse (see the module docstring)."""
    selection = over.pop("selection", row_selection())
    source = leg(which=SOURCE_TO_MAPPING, endpoint_binding=SOURCE_BINDING,
                 mapping_binding=MAP_BINDING,
                 endpoint_column="acct_no" if SOURCE_REF == CIB else "counter_party_acct_no",
                 mapping_column="acct_no" if SOURCE_REF == CIB else "ext_acct_ref",
                 **over.pop("source", {}))
    target = leg(which=MAPPING_TO_TARGET, endpoint_binding=TARGET_BINDING,
                 mapping_binding=MAP_BINDING,
                 endpoint_column="counter_party_acct_no" if SOURCE_REF == CIB else "acct_no",
                 mapping_column="ext_acct_ref" if SOURCE_REF == CIB else "acct_no",
                 cross_catalog=True, **over.pop("target", {}))
    kwargs = dict(
        crosswalk_definition_revision_id=DEFINITION.revision_id,
        source_leg=source, target_leg=target,
        mapping=mapping_observation(MAP_BINDING, duplicate_source_tuple_count=1,
                                    max_rows_per_source_tuple=2),
        scope_id=PRODUCTION_SCOPE.scope_id,
        matched_source_distinct=3, unmatched_source_distinct=0,
        matched_target_distinct=3, unmatched_target_distinct=0,
        composed_row_count=3,
        # 1:1 forward; the reverse landing tuple is duplicated, so the reverse direction is a
        # MEASURED fan-out — an answer, and a hard one.
        source_to_target_max_matches=1, target_to_source_max_matches=2,
        observed_at=NOW,
        mapping_temporal_policy_revision_id=TEMPORAL_POLICY,
        mapping_row_selection_hash=selection.content_hash)
    kwargs.update(over)
    return compose_crosswalk_observation(**kwargs)


def leg_pins() -> tuple[JoinLegPinV1, JoinLegPinV1]:
    """Two pins in the shapes their real owners produce: one same-catalog, one cross-catalog.

    The same-catalog leg pins NO fact key and NO realization revision — ``JoinLegPinV1`` refuses
    that pretence structurally, and the ordinary crosswalk shape is exactly one of each.
    """
    source_pin = JoinLegPinV1(
        kind=JoinLegKind.SAME_CATALOG,
        plan_hash="plan-" + "1" * 16,
        from_dataset_ref=SOURCE_REF, to_dataset_ref=MAP,
        from_binding_revision_id=SOURCE_BINDING.binding_revision_id,
        to_binding_revision_id=MAP_BINDING.binding_revision_id,
        read_set_hash="read-" + "1" * 16)
    target_pin = JoinLegPinV1(
        kind=JoinLegKind.CROSS_CATALOG,
        plan_hash="plan-" + "2" * 16,
        from_dataset_ref=MAP, to_dataset_ref=TARGET_REF,
        from_binding_revision_id=MAP_BINDING.binding_revision_id,
        to_binding_revision_id=TARGET_BINDING.binding_revision_id,
        read_set_hash="read-" + "2" * 16,
        fact_keys=("ebf-cwx-leg2",),
        realization_revision_ids=("pbr_" + "e" * 60,),
        dependency_snapshot_ids=("dps_" + "f" * 60,))
    return source_pin, target_pin


def admitted(*, scope=PRODUCTION_SCOPE, selection=None, obs=None,
             policy=None, **over) -> AdmittedCrosswalkV1:
    """One assembled :class:`AdmittedCrosswalkV1` — definition, measurement, decision, execution."""
    selection = row_selection() if selection is None else selection
    measured = observation(selection=selection, scope_id=scope.scope_id) if obs is None else obs
    decision = evaluate_crosswalk_admission(
        crosswalk_definition_revision_id=DEFINITION.revision_id,
        scope=scope, policy=policy or CrosswalkAdmissionPolicyV1(), now=NOW,
        observation=measured,
        mapping_binding_revision_id=MAP_BINDING.binding_revision_id)
    source_pin, target_pin = leg_pins()
    execution = admitted_crosswalk_execution(
        decision, source_leg=source_pin, target_leg=target_pin,
        mapping_binding_revision_id=MAP_BINDING.binding_revision_id,
        scope=scope, observation=measured,
        mapping_temporal_policy_revision_id=TEMPORAL_POLICY)
    kwargs = dict(definition=DEFINITION, execution=execution, decision=decision,
                  mapping_row_selection=selection, observation=measured)
    kwargs.update(over)
    return AdmittedCrosswalkV1(**kwargs)


def unmeasured(*, scope=SANDBOX_SCOPE) -> AdmittedCrosswalkV1:
    """"Discoverable, unmeasured" — a usable state everywhere EXCEPT a compiled traversal."""
    decision = evaluate_crosswalk_admission(
        crosswalk_definition_revision_id=DEFINITION.revision_id,
        scope=scope, policy=CrosswalkAdmissionPolicyV1(), now=NOW, observation=None)
    source_pin, target_pin = leg_pins()
    execution = admitted_crosswalk_execution(
        decision, source_leg=source_pin, target_leg=target_pin,
        mapping_binding_revision_id=MAP_BINDING.binding_revision_id,
        scope=scope, observation=None)
    return AdmittedCrosswalkV1(definition=DEFINITION, execution=execution, decision=decision)


# ── the expression a crosswalk traversal is planned for ──────────────────────────────────────────

def window(**over) -> WindowPolicy:
    kwargs = dict(
        event_time_ref=CIB_OPENED, basis=WindowBasis.TRAILING, length=30, unit=WindowUnit.DAY,
        start_inclusive=Inclusivity.INCLUSIVE, end_inclusive=Inclusivity.EXCLUSIVE,
        timezone=TZ, empty_window=EmptyWindowResult.NULL, null_input=NullInput.IGNORE)
    kwargs.update(over)
    return WindowPolicy(**kwargs)


def expression(**over) -> AggregateExpression:
    """Accounts opened in a trailing window, to be grained on the correspondent's own scheme."""
    kwargs = dict(
        aggregation=AggregateFunction.COUNT_ROWS, operand=None,
        source_relation=SourceRelation(table_ref=SOURCE_REF), filter=None, window=window())
    kwargs.update(over)
    return AggregateExpression(**kwargs)


def reverse_expression() -> AggregateExpression:
    """The same shape planned the OTHER way — from the target side back to the source side."""
    return replace(
        expression(),
        source_relation=SourceRelation(table_ref=TARGET_REF),
        window=window(event_time_ref=f"{TARGET_REF}.party_lei"))


#: The grain a forward traversal reaches, and the one a reverse traversal reaches.
FORWARD_GRAIN = (f"{TARGET_REF}.counter_party_acct_no" if TARGET_REF == FTR
                 else f"{TARGET_REF}.acct_no",)
REVERSE_GRAIN = (f"{SOURCE_REF}.acct_no" if SOURCE_REF == CIB
                 else f"{SOURCE_REF}.counter_party_acct_no",)


# ── the whole group: one feature, compiled, authorized, planned and wired ────────────────────────

FEATURE = "accounts_opened_30d"
GROUP = "counterparty_account_daily"
CADENCE = CadenceDecl(period=CadencePeriod.DAILY, timezone=TZ,
                      business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)
DECLARER = IdentityEnvelope(
    subject="user:asha", actor_kind="human", authenticated=False, auth_method="test",
    role_claims=("feature_engineer",))

SPINE_DECLARATION = SpineSourceDeclarationV1(
    entity=ENTITY,
    source_table_ref=FTR,
    ordered_key_refs=(FTR_ACCT,),
    population_semantics=PopulationSemantics.CURRENT_COMPLETE_POPULATION,
    availability_ref=f"{FTR}.load_ts",
    snapshot_policy=CurrentSnapshot(observed_snapshot_ref="2026-08-04"),
    declared_by=DECLARER,
    declaration_reason="the correspondent's own account scheme is the published population",
    declaration_version=1,
    recorded_at="2026-08-04T09:00:00+00:00",
    declaration_record_id="spine-decl-cwx")


def formula() -> TypedFormulaV1:
    """COUNT_ROWS over CIB accounts, published one row per FTR account.

    ``COUNT_ROWS`` deliberately: it has no operand, so the fixture needs no governed
    ``logical_representation`` on a third table, and the traversal — not the arithmetic — is what
    this acceptance is about.
    """
    return TypedFormulaV1(
        formula_schema_version=FORMULA_SCHEMA_VERSION,
        operation_grammar_version=OPERATION_GRAMMAR_VERSION,
        output_policy_version=OUTPUT_POLICY_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
        grain=Grain(entity=ENTITY, keys=FORWARD_GRAIN),
        body=UnaryBody(expr=expression()),
        parameters=(), decimal=DECIMAL,
        output=FormulaOutputPolicyV1(
            output_type="integer", unit=None, currency=None,
            output_additivity=AdditivityClass.NON_ADDITIVE, external_type_required=False))


def admitted_feature() -> AdmittedFeature:
    resolved = formula()
    return AdmittedFeature(
        feature_name=FEATURE, formula=resolved,
        formula_content_hash=formula_content_hash(resolved),
        intent=fixtures.intent_for("distinct_merchant_count_90d"),
        authoring_run_id="run-cwx-0001")


def compiled_group(db, *, crosswalks=None, roles=("feature_engineer",),
                   execution_tier=ExecutionTier.PRODUCTION) -> NodeAssemblyInputs:
    """Every stage REAL: compile -> Gate 2 -> contract -> physical type -> plan -> datasets.

    Returns the same ``NodeAssemblyInputs`` ``compile.chain`` hands to ``assemble_nodes``, so a
    render test drives the shipped assembler rather than a hand-built node list.
    """
    artifact = admitted_feature()
    ir = compile_ir(db, artifact, roles=roles, spine_decl=SPINE_DECLARATION, inventory=INVENTORY,
                    crosswalks=(admitted(),) if crosswalks is None else crosswalks,
                    execution_tier=execution_tier)
    assert not isinstance(ir, MaterializationRefused), getattr(ir, "detail", ir)
    authorized = authorize_compilation(db, (ir,), ir.spine, roles=roles)
    assert not isinstance(authorized, MaterializationRefused), getattr(authorized, "detail",
                                                                      authorized)
    group = derive_group_contract(db, authorized, cadence=CADENCE,
                                  availability_promise=AvailabilityPromiseV1(calendar_days=1))
    assert isinstance(group, ContractGroup), group
    physical = resolve_physical_type(
        artifact.formula,
        operand_types={e.expr_path: e.operand_type for e in ir.expressions})
    assert isinstance(physical, PhysicalType), physical
    plan = build_group_plan(group, (PlannedFeature(
        column_name=ir.feature_name, ir_hash=ir_hash(ir), physical_type=physical),),
        logical_group_name=GROUP)
    spine_input = derive_requirement(db, INVENTORY, table_ref=FTR)
    return NodeAssemblyInputs(
        authorized=authorized, plan=plan, contract=group.contract,
        admitted={FEATURE: artifact}, spine_input=spine_input,
        datasets=project_datasets(authorized, plan, spine_input=spine_input))


def render(inputs: NodeAssemblyInputs, *, publisher_selection=None) -> SealedProject:
    """The sealed Kedro project, assembled by the SHIPPED node assembler."""
    return render_project(
        inputs.authorized, inputs.plan, environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=inputs.spine_input,
        nodes=assemble_nodes(inputs), publisher_selection=publisher_selection)
