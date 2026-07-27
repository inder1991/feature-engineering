"""Spec A Task 10 — §9's packing list, and the manifest evidence that assembly consumes.

**Completeness is proven by MANIFEST, not by schema (§9).** A matching output schema cannot show
that a column was produced by the expected computation: the same shape comes out of a different
`ir_hash`, out of a previous run, or out of a staging path somebody reused.
`test_a_right_schema_with_a_WRONG_ir_hash_still_fails` is the whole point of the gate — every
column present, every type right, every nullability right, and the group still refuses.

**A manifest is bound to THIS generation, run and business date.** Without that binding a reused
staging path can leave an OLDER successful manifest whose `ir_hash` still matches, and assembly
would publish stale output that passes every other check. `STALE_STAGING_MANIFEST` exists for
exactly that, so the stale tests deliberately use a manifest whose `ir_hash` is CORRECT.

**The three system columns (§10.2)** are part of `expected_schema` and therefore part of §9's schema
gate. They are added once at assembly and never in per-feature staging — which is Task 13/14's
obligation; what is testable here is that the plan says they belong in the published schema, exactly
once each.

The contract, the spine declaration and the compile helpers come from `test_ir`/`test_contract`'s
own fixtures rather than being re-declared: a second definition of the governed catalog is a second
chance to disagree about what a contract is, and the two real-fixture tests at the end are only
worth anything if the group they plan is the group Task 9 really derives.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_ir import (
    CUSTOMERS_CIF,
    DECLARATION,
    INVENTORY,
    TXN_AMT,
    _admitted,
    _compile,
    _ok,
    seed_catalog,
)

from featuregen.formula.schema import OverflowBehavior, RoundingMode
from featuregen.materialize.admission import FeatureNamePlanError
from featuregen.materialize.classify import CLASSIFICATION_POLICY_VERSION
from featuregen.materialize.codes import MaterializationRefused, ValidationGateCode
from featuregen.materialize.contract import (
    BUSINESS_DT_COLUMN,
    DEFAULT_RETENTION_CLASS,
    RETENTION_POLICY_VERSION,
    AvailabilityPromiseV1,
    BackfillBoundary,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
    ContractGroup,
    LandingPitSemantics,
    MaterializationContractV1,
    PublicationPolicy,
    contract_hash,
    derive_group_contract,
)
from featuregen.materialize.group_plan import (
    SYSTEM_COLUMN_SQL_TYPE,
    SYSTEM_COLUMNS,
    ColumnRole,
    FeatureGroupPlanV1,
    PlannedFeature,
    StagingManifestV1,
    StagingStatus,
    build_group_plan,
    check_completeness,
    expected_schema,
    expected_schema_hash,
    group_plan_hash,
)
from featuregen.materialize.ir import authorize_compilation, ir_hash
from featuregen.materialize.physical_types import (
    PHYSICAL_TYPE_POLICY_VERSION,
    PhysicalType,
    resolve_physical_type,
)
from featuregen.overlay.upload.field_resolution import resolve_and_project

_ROLES = ("feature_engineer",)
GROUP = "cif_daily"
GENERATION = "gen-0001"
RUN = "run-0001"
BUSINESS_DT = "2026-07-27"

CADENCE = CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                      business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)

#: A count and a sum: integral/non-null against fixed-point/nullable, so a test that moves either
#: half of the type decision has something to move it away from.
BIGINT = PhysicalType(sql_type="BIGINT", nullable=False, rounding=None, overflow=None)
DECIMAL = PhysicalType(sql_type="DECIMAL(38,6)", nullable=True, rounding=RoundingMode.HALF_EVEN,
                       overflow=OverflowBehavior.ERROR)

SUM_30D = "total_debit_amount_30d"
COUNT_90D = "distinct_merchant_count_90d"
RATIO_90D = "cross_border_value_ratio_90d"


# ── values, built without a catalog: Task 10 is pure ─────────────────────────────────────────────

def _contract(*, cadence=CADENCE, ordered_keys=(CUSTOMERS_CIF,),
              business_dt_column=BUSINESS_DT_COLUMN) -> MaterializationContractV1:
    """A contract of the shape Task 9 derives — the only inputs Task 10 reads are the landing key,
    the business-date column and the hash."""
    return MaterializationContractV1(
        entity="customer",
        ordered_keys=ordered_keys,
        pit_semantics=LandingPitSemantics(
            entity_keys=ordered_keys, business_dt_column=business_dt_column,
            cutoff_timezone=cadence.timezone, cutoff_time=cadence.business_date_cutoff,
            availability_basis_class=("posted_at",)),
        sensitivity_class="internal",
        access_requirements=(),
        retention_class=DEFAULT_RETENTION_CLASS,
        retention_policy_version=RETENTION_POLICY_VERSION,
        availability_promise=AvailabilityPromiseV1(calendar_days=1),
        cadence=cadence,
        publication_policy=PublicationPolicy.ATOMIC_GROUP,
        backfill_boundary=BackfillBoundary.GROUP_LEVEL,
        spine=DECLARATION,
        classification_policy_version=CLASSIFICATION_POLICY_VERSION,
        physical_type_policy_version=PHYSICAL_TYPE_POLICY_VERSION)


def _group(*names, contract=None) -> ContractGroup:
    resolved = _contract() if contract is None else contract
    return ContractGroup(contract_hash=contract_hash(resolved), contract=resolved,
                         feature_names=tuple(sorted(names)))


def _feature(name, *, ir_hash="ir-" + "a" * 8, physical_type=BIGINT) -> PlannedFeature:
    return PlannedFeature(column_name=name, ir_hash=ir_hash, physical_type=physical_type)


def _plan(*features, group=None, logical_group_name=GROUP) -> FeatureGroupPlanV1:
    planned = features or (_feature(SUM_30D, ir_hash="ir-sum", physical_type=DECIMAL),
                           _feature(COUNT_90D, ir_hash="ir-count"))
    resolved = _group(*[f.column_name for f in planned]) if group is None else group
    return build_group_plan(resolved, planned, logical_group_name=logical_group_name)


def _manifest(feature: PlannedFeature, **overrides) -> StagingManifestV1:
    fields = {
        "intent_feature_name": feature.column_name,
        "ir_hash": feature.ir_hash,
        "generation_id": GENERATION,
        "run_id": RUN,
        "business_dt": BUSINESS_DT,
        "generated_project_hash": "proj-0001",
        "sandbox_execution_hash": "exec-0001",
        "output_location": f"hdfs://nn/staging/{GENERATION}/{feature.column_name}",
        "schema_hash": "schema-0001",
        "row_count": 12,
        "status": StagingStatus.COMPLETED,
    }
    fields.update(overrides)
    return StagingManifestV1(**fields)


def _manifests(plan: FeatureGroupPlanV1) -> list[StagingManifestV1]:
    return [_manifest(feature) for feature in plan.features]


def _check(plan, manifests) -> tuple[ValidationGateCode, ...]:
    """Every gate code the completeness check reported, in the order it reported them."""
    return tuple(failure.code
                 for failure in check_completeness(plan, manifests, generation_id=GENERATION,
                                                   run_id=RUN, business_dt=BUSINESS_DT))


def _named(plan: FeatureGroupPlanV1, name: str):
    return next(column for column in expected_schema(plan) if column.name == name)


# ══ §10.2 — the three system columns ═════════════════════════════════════════════════════════════


def test_expected_schema_carries_the_THREE_system_columns() -> None:
    """§10.2: the generation marker lives in the DATA, so it is part of the schema §9 gates."""
    plan = _plan()
    system = tuple(column.name for column in expected_schema(plan)
                   if column.role is ColumnRole.SYSTEM)
    assert system == ("__generation_id", "__generated_project_hash", "__sandbox_execution_hash")
    assert system == SYSTEM_COLUMNS


def test_each_system_column_appears_EXACTLY_once() -> None:
    """They are added once at assembly (§10.2). A plan that named one twice would make the
    'exactly one copy' assertion Task 14 has to make unprovable."""
    names = [column.name for column in expected_schema(_plan())]
    assert [names.count(system) for system in SYSTEM_COLUMNS] == [1, 1, 1]


def test_a_feature_can_never_collide_with_a_system_column() -> None:
    """Structural, not a rule to remember: a feature column is a Hive identifier, which must start
    with a LETTER, so a name beginning `__` cannot be admitted at all."""
    with pytest.raises(FeatureNamePlanError):
        _feature("__generation_id")


def test_the_system_columns_are_typed_and_NOT_NULL() -> None:
    """We render the assembly node that writes them, so their type is decided here rather than
    read from a catalog — and a NULL generation marker would defeat §10.2's whole purpose.

    Pinned BY VALUE, not against the constant: `sql_type == SYSTEM_COLUMN_SQL_TYPE` is satisfied by
    any value the constant happens to hold, so it would assert that the module agrees with itself.
    All three are opaque hex identifiers, so `STRING` is the claim being made."""
    assert SYSTEM_COLUMN_SQL_TYPE == "STRING"
    for name in SYSTEM_COLUMNS:
        column = _named(_plan(), name)
        assert (column.sql_type, column.nullable) == ("STRING", False)


def test_expected_schema_is_keys_then_business_dt_then_features_then_system() -> None:
    plan = _plan()
    assert [column.name for column in expected_schema(plan)] == [
        "cif_id", "business_dt", COUNT_90D, SUM_30D, *SYSTEM_COLUMNS]
    assert [column.role for column in expected_schema(plan)] == [
        ColumnRole.ENTITY_KEY, ColumnRole.BUSINESS_DT, ColumnRole.FEATURE, ColumnRole.FEATURE,
        *[ColumnRole.SYSTEM] * 3]


def test_the_expected_schema_hash_moves_with_the_schema() -> None:
    two = _plan()
    three = _plan(*two.features, _feature(RATIO_90D, ir_hash="ir-ratio", physical_type=DECIMAL))
    assert expected_schema_hash(two) != expected_schema_hash(three)


def test_the_expected_schema_hash_moves_with_NULLABILITY_alone() -> None:
    """§9 gates nullability, so two schemas that differ only there are two schemas — a hash that
    ignored it would let `WRONG_NULLABILITY` pass the `SCHEMA_HASH_MISMATCH` gate."""
    strict = _plan(_feature(COUNT_90D, physical_type=BIGINT))
    loose = _plan(_feature(COUNT_90D, physical_type=dataclasses.replace(BIGINT, nullable=True)))
    assert _named(strict, COUNT_90D).sql_type == _named(loose, COUNT_90D).sql_type
    assert expected_schema_hash(strict) != expected_schema_hash(loose)


# ══ §6 — the resolved type AND its nullability ═══════════════════════════════════════════════════


def test_each_feature_column_carries_its_resolved_type_and_nullability() -> None:
    plan = _plan()
    assert (_named(plan, SUM_30D).sql_type, _named(plan, SUM_30D).nullable) \
        == ("DECIMAL(38,6)", True)
    assert (_named(plan, COUNT_90D).sql_type, _named(plan, COUNT_90D).nullable) == ("BIGINT", False)


def test_a_nullability_mismatch_is_EXPRESSIBLE() -> None:
    """§9 gates type AND nullability, so the plan must be able to say a column differs only there —
    otherwise `WRONG_NULLABILITY` is a code nothing can ever raise."""
    nullable_count = dataclasses.replace(BIGINT, nullable=True)
    plan = _plan(_feature(COUNT_90D, physical_type=nullable_count))
    other = _plan(_feature(COUNT_90D, physical_type=BIGINT))
    assert _named(plan, COUNT_90D).sql_type == _named(other, COUNT_90D).sql_type
    assert _named(plan, COUNT_90D).nullable != _named(other, COUNT_90D).nullable
    assert group_plan_hash(plan) != group_plan_hash(other)


def test_the_landing_key_and_business_dt_carry_NO_governed_type() -> None:
    """A RECORDED gap, asserted rather than papered over: §6 decides the type of a FEATURE column,
    and no governed source states `cif_id`'s physical type at compile time. Declaring one would be
    an invention that the §9 gate would then enforce against the real table."""
    plan = _plan()
    assert _named(plan, "cif_id").sql_type is None
    assert _named(plan, BUSINESS_DT_COLUMN).sql_type is None
    assert all(column.sql_type is not None for column in expected_schema(plan)
               if column.role in (ColumnRole.FEATURE, ColumnRole.SYSTEM))


def test_the_landing_key_and_business_dt_are_declared_NOT_NULL() -> None:
    """Not an invention: the published shape is one row per `(keys…, business_dt)`, and a NULL key
    or a NULL business date is not a landing key at all."""
    plan = _plan()
    assert _named(plan, "cif_id").nullable is False
    assert _named(plan, BUSINESS_DT_COLUMN).nullable is False


# ══ plan identity ════════════════════════════════════════════════════════════════════════════════


def test_adding_a_feature_changes_the_group_plan_hash() -> None:
    two = _plan()
    three = _plan(*two.features, _feature(RATIO_90D, ir_hash="ir-ratio", physical_type=DECIMAL))
    assert group_plan_hash(two) != group_plan_hash(three)


def test_a_different_ir_hash_is_a_different_plan() -> None:
    assert group_plan_hash(_plan(_feature(COUNT_90D, ir_hash="ir-a"))) \
        != group_plan_hash(_plan(_feature(COUNT_90D, ir_hash="ir-b")))


def test_the_contract_hash_is_part_of_plan_identity() -> None:
    """A plan under a different contract publishes a differently-meaning row for the same columns."""
    elsewhere = _contract(cadence=dataclasses.replace(CADENCE, timezone="Europe/London"))
    features = (_feature(COUNT_90D),)
    here = _plan(*features)
    there = _plan(*features, group=_group(COUNT_90D, contract=elsewhere))
    assert here.materialization_contract_hash != there.materialization_contract_hash
    assert group_plan_hash(here) != group_plan_hash(there)


def test_the_LANDING_KEY_is_part_of_plan_identity() -> None:
    """A row keyed by a different column is a different row, whatever the feature columns say."""
    other_key = f"{CUSTOMERS_CIF.rsplit('.', 1)[0]}.household_id"
    features = (_feature(COUNT_90D),)
    here = _plan(*features)
    there = _plan(*features, group=_group(COUNT_90D, contract=_contract(ordered_keys=(other_key,))))
    assert [column.name for column in expected_schema(there)][0] == "household_id"
    assert group_plan_hash(here) != group_plan_hash(there)


def test_the_BUSINESS_DT_column_is_part_of_plan_identity() -> None:
    features = (_feature(COUNT_90D),)
    here = _plan(*features)
    there = _plan(*features, group=_group(
        COUNT_90D, contract=_contract(business_dt_column="as_of_dt")))
    assert [column.name for column in expected_schema(there)][1] == "as_of_dt"
    assert group_plan_hash(here) != group_plan_hash(there)


def test_the_logical_group_name_is_part_of_plan_identity() -> None:
    """It decides the published table, which the rendered project hard-codes (§7)."""
    features = (_feature(COUNT_90D),)
    assert group_plan_hash(_plan(*features)) \
        != group_plan_hash(_plan(*features, logical_group_name="cif_monthly"))


def test_the_plan_hash_does_not_depend_on_the_order_features_were_supplied() -> None:
    sum_first = _plan(_feature(SUM_30D, physical_type=DECIMAL), _feature(COUNT_90D))
    count_first = _plan(_feature(COUNT_90D), _feature(SUM_30D, physical_type=DECIMAL))
    assert group_plan_hash(sum_first) == group_plan_hash(count_first)


def test_the_physical_type_policy_version_is_carried_and_hashed() -> None:
    """§6: the resolved type and the policy version both enter `group_plan_hash`. A type decided
    under a different rule is a different column even when the type string matches."""
    plan = _plan()
    assert plan.physical_type_policy_version == PHYSICAL_TYPE_POLICY_VERSION
    assert plan.identity_payload()["physical_type_policy_version"] == PHYSICAL_TYPE_POLICY_VERSION


def test_the_system_columns_are_part_of_plan_identity() -> None:
    """Adding a fourth system column changes the published schema, so it must re-key the plan."""
    assert _plan().identity_payload()["system_columns"] == list(SYSTEM_COLUMNS)


def test_the_plan_carries_no_run_time_value() -> None:
    """`generation_id`, `run_id` and `business_dt` are run facts (§3.3): a plan that carried one
    would compile to a different artifact every day."""
    payload = str(_plan().identity_payload())
    assert GENERATION not in payload and RUN not in payload and BUSINESS_DT not in payload


# ══ what `build_group_plan` refuses ══════════════════════════════════════════════════════════════


def test_the_planned_features_must_describe_EXACTLY_the_group() -> None:
    """The same fail-open closure Task 8.1 drew: a feature omitted from the plan is a feature the
    group publishes nothing for, and one nobody asked for is a column with no contract."""
    group = _group(SUM_30D, COUNT_90D)
    with pytest.raises(ValueError, match="exactly"):
        build_group_plan(group, (_feature(SUM_30D),), logical_group_name=GROUP)
    with pytest.raises(ValueError, match="exactly"):
        build_group_plan(group, (_feature(SUM_30D), _feature(COUNT_90D), _feature(RATIO_90D)),
                         logical_group_name=GROUP)


def test_two_features_normalizing_to_ONE_column_is_a_plan_error() -> None:
    with pytest.raises(FeatureNamePlanError):
        build_group_plan(_group(COUNT_90D), (_feature(COUNT_90D), _feature(COUNT_90D.upper())),
                         logical_group_name=GROUP)


def test_a_feature_colliding_with_the_landing_KEY_is_a_plan_error() -> None:
    """`cif_id` is already a column of the published row; a feature of that name would overwrite
    the key it is keyed by."""
    with pytest.raises(FeatureNamePlanError, match="cif_id"):
        build_group_plan(_group("cif_id"), (_feature("cif_id"),), logical_group_name=GROUP)


def test_a_feature_colliding_with_BUSINESS_DT_is_a_plan_error() -> None:
    with pytest.raises(FeatureNamePlanError, match=BUSINESS_DT_COLUMN):
        build_group_plan(_group(BUSINESS_DT_COLUMN), (_feature(BUSINESS_DT_COLUMN),),
                         logical_group_name=GROUP)


def test_a_group_name_that_is_not_a_hive_identifier_is_a_plan_error() -> None:
    with pytest.raises(FeatureNamePlanError):
        _plan(logical_group_name="1 daily")


def test_a_landing_key_that_names_a_TABLE_is_refused() -> None:
    """The published row is one per (keys…, business_dt); a key that is a whole table names no
    column of it."""
    table_ref = CUSTOMERS_CIF.rsplit(".", 1)[0]
    with pytest.raises(ValueError, match="TABLE"):
        build_group_plan(_group(COUNT_90D, contract=_contract(ordered_keys=(table_ref,))),
                         (_feature(COUNT_90D),), logical_group_name=GROUP)


def test_a_bare_list_of_features_cannot_be_planned_without_a_GROUP() -> None:
    """§5.1's grouping is the evidence that these features share ONE contract; planning without it
    would publish columns nobody showed to agree."""
    with pytest.raises(TypeError, match="ContractGroup"):
        build_group_plan((_feature(COUNT_90D),), (_feature(COUNT_90D),),
                         logical_group_name=GROUP)


def test_a_REFUSAL_cannot_be_passed_as_a_resolved_type() -> None:
    """`resolve_physical_type` returns `PhysicalType | MaterializationRefused`; a caller that
    forgot to check would otherwise plan a column whose type was refused."""
    from featuregen.materialize.codes import CompilationRefusalCode
    refusal = MaterializationRefused(CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED, "nope")
    with pytest.raises(TypeError):
        PlannedFeature(column_name=COUNT_90D, ir_hash="ir-x", physical_type=refusal)


def test_a_planned_feature_needs_an_ir_hash() -> None:
    """The plan is what §9 compares a manifest's `ir_hash` against; a blank one would compare
    nothing to nothing and pass."""
    with pytest.raises(ValueError, match="ir_hash"):
        PlannedFeature(column_name=COUNT_90D, ir_hash="  ", physical_type=BIGINT)


# ══ §9 — completeness is proven by MANIFEST, never by schema ═════════════════════════════════════


def test_a_complete_matching_manifest_set_passes() -> None:
    plan = _plan()
    assert check_completeness(plan, _manifests(plan), generation_id=GENERATION, run_id=RUN,
                              business_dt=BUSINESS_DT) == ()


def test_a_right_schema_with_a_WRONG_ir_hash_still_FAILS() -> None:
    """§9's headline. The manifest describes a column of the right name, the right row count and a
    schema hash the plan would accept — and the computation behind it is not the planned one, which
    NOTHING about the output shape could ever reveal."""
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], ir_hash="ir-from-another-computation")
    assert _check(plan, manifests) == (ValidationGateCode.IR_HASH_MISMATCH,)


def test_a_manifest_from_another_GENERATION_is_stale() -> None:
    """Its `ir_hash` MATCHES — that is the point. A reused staging path leaves an older successful
    manifest, and every other check would pass on it."""
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], generation_id="gen-0000")
    assert manifests[0].ir_hash == plan.features[0].ir_hash
    assert _check(plan, manifests) == (ValidationGateCode.STALE_STAGING_MANIFEST,)


def test_a_manifest_from_another_RUN_is_stale() -> None:
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], run_id="run-0000")
    assert _check(plan, manifests) == (ValidationGateCode.STALE_STAGING_MANIFEST,)


def test_a_manifest_for_another_BUSINESS_DATE_is_stale() -> None:
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], business_dt="2026-07-26")
    assert _check(plan, manifests) == (ValidationGateCode.STALE_STAGING_MANIFEST,)


def test_staleness_is_judged_BEFORE_the_ir_hash() -> None:
    """A manifest from another run is not evidence about this one at all, so judging its `ir_hash`
    would report a verdict on a computation nobody asked about."""
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], run_id="run-0000", ir_hash="ir-elsewhere")
    assert _check(plan, manifests) == (ValidationGateCode.STALE_STAGING_MANIFEST,)


def test_a_missing_manifest_is_refused() -> None:
    plan = _plan()
    assert _check(plan, _manifests(plan)[1:]) == (ValidationGateCode.MISSING_STAGING_MANIFEST,)


def test_a_DUPLICATE_manifest_is_refused() -> None:
    """Exactly one per planned feature (§9). Two say the staging path was written twice, and
    picking either would be picking which computation published."""
    plan = _plan()
    manifests = _manifests(plan)
    assert _check(plan, [*manifests, manifests[0]]) \
        == (ValidationGateCode.DUPLICATE_STAGING_MANIFEST,)


def test_a_duplicate_is_refused_even_when_the_two_are_IDENTICAL() -> None:
    """Byte-equal duplicates still mean two writes reached one staging name."""
    plan = _plan()
    manifests = _manifests(plan)
    assert ValidationGateCode.DUPLICATE_STAGING_MANIFEST in _check(plan, [manifests[0], *manifests])


def test_a_FAILED_manifest_is_refused_as_an_INCOMPLETE_COMPUTATION() -> None:
    """The staging job recorded its own failure, so the column was never computed — a computation
    verdict, not a missing record."""
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], status=StagingStatus.FAILED)
    assert _check(plan, manifests) == (ValidationGateCode.INCOMPLETE_COMPUTATION,)


def test_an_UNEXPECTED_manifest_is_refused() -> None:
    """Staging holds output for a feature this plan does not contain — an extra column at
    assembly, and evidence the staging area belongs to another plan."""
    plan = _plan()
    stranger = _manifest(_feature("some_other_feature", ir_hash="ir-other"))
    assert _check(plan, [*_manifests(plan), stranger]) == (ValidationGateCode.UNEXPECTED_COLUMN,)


def test_an_unexpected_manifest_from_another_run_is_reported_as_STALE() -> None:
    """Staleness is judged over the WHOLE staging area, not only over planned features: a manifest
    from another run sitting in this generation's path is the reused-path hazard itself."""
    plan = _plan()
    stranger = _manifest(_feature("some_other_feature", ir_hash="ir-other"), run_id="run-0000")
    assert _check(plan, [*_manifests(plan), stranger]) \
        == (ValidationGateCode.STALE_STAGING_MANIFEST,)


def test_every_failing_feature_is_reported_not_just_the_first() -> None:
    """A gate that stopped at the first failure would send an operator round the loop once per
    broken feature."""
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], ir_hash="ir-wrong")
    manifests[1] = dataclasses.replace(manifests[1], status=StagingStatus.FAILED)
    assert _check(plan, manifests) == (ValidationGateCode.IR_HASH_MISMATCH,
                                       ValidationGateCode.INCOMPLETE_COMPUTATION)


def test_a_failure_names_the_feature_and_carries_no_data_value() -> None:
    plan = _plan()
    manifests = _manifests(plan)
    manifests[0] = dataclasses.replace(manifests[0], ir_hash="ir-wrong")
    failure = check_completeness(plan, manifests, generation_id=GENERATION, run_id=RUN,
                                 business_dt=BUSINESS_DT)[0]
    assert plan.features[0].column_name in failure.detail
    assert plan.features[0].ir_hash in failure.detail


def test_the_gate_cannot_be_run_without_a_run_binding() -> None:
    """A blank generation would make every manifest 'bound to this run' and STALE unreachable."""
    plan = _plan()
    for blank in ({"generation_id": " "}, {"run_id": ""}, {"business_dt": "  "}):
        kwargs = {"generation_id": GENERATION, "run_id": RUN, "business_dt": BUSINESS_DT, **blank}
        with pytest.raises(ValueError):
            check_completeness(plan, _manifests(plan), **kwargs)


def test_the_staging_status_vocabulary_is_CLOSED() -> None:
    assert {status.value for status in StagingStatus} == {"completed", "failed"}
    with pytest.raises(ValueError):
        _manifest(_feature(COUNT_90D), status="in_progress")


def test_a_manifest_status_may_be_written_as_its_plain_string() -> None:
    """The rendered pipeline writes JSON, so a manifest read back carries `"completed"` — it must
    canonicalize to the enum rather than becoming a second, uncomparable spelling."""
    assert _manifest(_feature(COUNT_90D), status="completed").status is StagingStatus.COMPLETED


def test_a_manifest_row_count_cannot_be_negative() -> None:
    with pytest.raises(ValueError):
        _manifest(_feature(COUNT_90D), row_count=-1)


# ══ the real group, really derived ═══════════════════════════════════════════════════════════════


@pytest.fixture
def catalog(db):
    """Task 7's governed catalog, PLUS the one governed type decision Task 8.1's gate needs.

    `seed_catalog` governs grain, entity and availability but attests no `logical_representation`,
    so a SUM over it refuses with `OUTPUT_TYPE_NOT_GOVERNED` — correctly, and uselessly for a
    composition test. The DECISION is made through the real machinery (`record_field_evidence` +
    `resolve_and_project`, as `fixtures.seed_materialize_catalog` does), because a flat insert would
    skip the lifecycle the governed reader derives its status from and the resolved type would then
    be a fiction.

    The flat `graph_node.data_type` is then written to the decided value, and that is not a
    shortcut: `test_ir`'s catalog is hand-built rather than uploaded, so no projection ever filled
    that column — and C1 compares the decision against it and fails CLOSED (`hash_mismatch` →
    `OperandTypeStatus.UNAVAILABLE`) when the two disagree. Completing the projection is what makes
    the governed read answer the decision that was actually made.
    """
    seed_catalog(db)
    fixtures._attest(db, TXN_AMT, "logical_representation", "numeric")
    resolve_and_project(db, source="hdfc", logical_refs=[TXN_AMT])
    db.execute(
        "UPDATE graph_node SET data_type = 'numeric' "
        "WHERE catalog_source = 'hdfc' AND object_ref = 'public.transactions.txn_amt'")
    return db


def _real_plan(db, *names):
    """Compile the named worked features, derive their ONE contract, and plan them — every input
    produced by the machinery that will really produce it."""
    irs = [_ok(_compile(db, name)) for name in names]
    authorization = authorize_compilation(db, irs, irs[0].spine, roles=_ROLES)
    group = derive_group_contract(db, authorization, cadence=CADENCE,
                                  availability_promise=AvailabilityPromiseV1(calendar_days=1))
    assert isinstance(group, ContractGroup), group
    features = []
    for ir in irs:
        resolved = resolve_physical_type(
            _admitted(ir.feature_name).formula,
            operand_types={e.expr_path: e.operand_type for e in ir.expressions})
        assert isinstance(resolved, PhysicalType), resolved
        features.append(PlannedFeature(column_name=ir.feature_name, ir_hash=ir_hash(ir),
                                       physical_type=resolved))
    return build_group_plan(group, tuple(features), logical_group_name=GROUP)


def test_a_real_group_plans_its_real_columns(catalog, db) -> None:
    """The composition test: Task 7's IR, Task 8.1's resolved type and Task 9's group contract all
    reach the plan without an adapter in between."""
    plan = _real_plan(db, SUM_30D, COUNT_90D)
    assert [column.name for column in expected_schema(plan)] == [
        "cif_id", "business_dt", COUNT_90D, SUM_30D, *SYSTEM_COLUMNS]
    assert _named(plan, COUNT_90D).sql_type == "BIGINT"
    assert _named(plan, SUM_30D).sql_type.startswith("DECIMAL(")


def test_a_real_plans_ir_hash_is_what_the_gate_compares(catalog, db) -> None:
    """The value in the plan is `ir_hash(ir)` itself, so a rendered manifest carrying that IR's
    hash passes and one carrying any other does not."""
    plan = _real_plan(db, COUNT_90D)
    real = ir_hash(_ok(_compile(db, COUNT_90D)))
    assert plan.features[0].ir_hash == real
    assert _check(plan, [_manifest(plan.features[0])]) == ()
    assert _check(plan, [_manifest(plan.features[0], ir_hash=real[::-1])]) \
        == (ValidationGateCode.IR_HASH_MISMATCH,)


def test_the_planned_column_is_the_ADMITTED_feature_name(catalog, db) -> None:
    """One normalizer, not two: the plan re-applies `admission.hive_identifier`, which is
    idempotent, so the published column is the column Gate 1 already named."""
    ir = _ok(_compile(db, RATIO_90D))
    assert _real_plan(db, RATIO_90D).features[0].column_name == ir.feature_name
    assert INVENTORY.environment_id  # the compile really used the shared inventory fixture
