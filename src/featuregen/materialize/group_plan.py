"""Spec §9 / §10.2 — the group's packing list, and the evidence assembly is allowed to trust.

**Completeness is proven by MANIFEST, never by schema (§9).** A matching output schema cannot show
which computation produced a column: the same shape comes out of a different ``ir_hash``, out of a
previous run, or out of a staging path somebody reused. So assembly consumes ONE
:class:`StagingManifestV1` per planned feature and refuses when one is missing, failed,
``ir_hash``-mismatched, duplicated — or is not about this run at all.

**A manifest is bound to THIS generation, THIS run and THIS business date.** Staging paths are
generation-scoped and immutable, and without that binding a reused path could leave an OLDER
successful manifest whose ``ir_hash`` still matches; assembly would then publish stale output that
passes every other check. That is what ``STALE_STAGING_MANIFEST`` is for, and it is judged BEFORE
the ``ir_hash``: a manifest from another run is not evidence about this one, so grading its
computation would report a verdict about a question nobody asked.

**The three system columns (§10.2) belong to the PUBLISHED schema, not to staging.** They are added
exactly once after assembly; per-feature staging carries only ``(keys…, business_dt, one feature
column)``, because a system column duplicated across every staging output collides at assembly.
They are part of :func:`expected_schema` and therefore part of §9's schema gate. Their leading
``__`` also makes a collision with a feature column structurally impossible — a feature name is an
:func:`~featuregen.materialize.admission.hive_identifier`, which must start with a LETTER.

**What the plan does NOT decide.** §6 decides the physical type of a FEATURE column. Nothing
governed states the physical type of the landing KEY or of ``business_dt`` at compile time — the
inventory carries partition-column types only (``inputs.PhysicalInputRequirement``), and the key's
type is whatever the spine's source table holds. So those columns carry ``sql_type=None``: an
invented ``STRING`` would be enforced by §9's gate against a real table and would refuse a correct
group over a claim nobody made. Their NULLABILITY is a different matter and IS decided — the
published shape is one row per ``(keys…, business_dt)``, and a NULL key is not a landing key.

**One normalizer, not two.** Column names go through ``admission.hive_identifier``, the same
function Gate 1 already used to name the feature. It is idempotent, so re-applying it to an admitted
name is a validation; a second regex here would be a second chance to disagree about which column a
feature occupies, and the disagreement would surface as a schema gate failing on a name nobody
chose. A post-normalization collision is a ``FeatureNamePlanError`` — a PLAN error, not a governed
refusal (spec §1.2: *never a silent overwrite*), and the same line ``admission`` already draws.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.materialize.admission import FeatureNamePlanError, hive_identifier
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.contract import ContractGroup
from featuregen.materialize.physical_types import PHYSICAL_TYPE_POLICY_VERSION, PhysicalType
from featuregen.overlay.upload.object_ref import parse_ref

__all__ = [
    "SYSTEM_COLUMNS",
    "SYSTEM_COLUMN_SQL_TYPE",
    "ColumnRole",
    "FeatureGroupPlanV1",
    "GateFailure",
    "PlannedColumn",
    "PlannedFeature",
    "StagingManifestV1",
    "StagingStatus",
    "build_group_plan",
    "check_completeness",
    "key_columns_from_refs",
    "expected_schema",
    "expected_schema_hash",
    "group_plan_hash",
]

#: §10.2's published generation metadata, in the order the spec names them. They live in the DATA
#: because the atomicity probe (§10.3) needs a marker visible in the SAME atomic state as the rows —
#: side metadata that moves separately proves nothing. Adding a fourth changes the published schema,
#: which is why the tuple enters ``group_plan_hash``.
SYSTEM_COLUMNS: tuple[str, ...] = (
    "__generation_id",
    "__generated_project_hash",
    "__sandbox_execution_hash",
)

#: The system columns' published type. Decided HERE rather than read from a catalog because we
#: render the assembly node that writes them: all three are hex/opaque identifiers, and a numeric or
#: date type would be a lie about a hash. Not nullable — a NULL generation marker defeats §10.2.
SYSTEM_COLUMN_SQL_TYPE = "STRING"


class ColumnRole(StrEnum):
    """WHY a column is in the published row — closed, and carried rather than inferred.

    §9 gates the three classes differently: a FEATURE column's type is decided by §6, a SYSTEM
    column's by this module, and an ENTITY_KEY / BUSINESS_DT column's by nobody at compile time (see
    the module docstring). A gate that could not tell them apart would either skip the typed columns
    or invent a type for the untyped ones.
    """

    ENTITY_KEY = "entity_key"
    BUSINESS_DT = "business_dt"
    FEATURE = "feature"
    SYSTEM = "system"


class StagingStatus(StrEnum):
    """What a per-feature staging job recorded about itself. CLOSED.

    §9 spells the field ``status: str``; a ``StrEnum`` IS a ``str``, so a manifest read back from
    the rendered pipeline's JSON still compares and serializes identically — while an unknown value
    fails at construction instead of silently being neither ``completed`` nor ``failed`` and
    slipping through a ``== "failed"`` test.
    """

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PlannedColumn:
    """One column of the published row, and everything §9 can honestly check about it.

    ``sql_type`` is ``None`` exactly where no governed source decides it (the landing key and
    ``business_dt``): the gate then checks presence, order and nullability for that column and
    checks the type only where a type was actually decided.
    """

    name: str
    role: ColumnRole
    sql_type: str | None
    nullable: bool

    def identity_payload(self) -> dict[str, Any]:
        return {"name": self.name, "role": self.role.value, "sql_type": self.sql_type,
                "nullable": self.nullable}


@dataclass(frozen=True, slots=True)
class PlannedFeature:
    """One feature's place in the group: its column, its computation, and its resolved type.

    ``ir_hash`` is the value §9 compares a staging manifest against, so it is required and
    non-blank: a blank one would compare nothing to nothing and pass. ``physical_type`` must be a
    resolved :class:`~featuregen.materialize.physical_types.PhysicalType` — ``resolve_physical_type``
    returns ``PhysicalType | MaterializationRefused``, and a caller who forgot to check would
    otherwise plan a column whose type was REFUSED.
    """

    column_name: str
    ir_hash: str
    physical_type: PhysicalType

    def __post_init__(self) -> None:
        # Normalizing here, not only in `build_group_plan`, so a hand-assembled `PlannedFeature`
        # cannot carry a name the published table could not express.
        object.__setattr__(self, "column_name", hive_identifier(self.column_name))
        if not isinstance(self.ir_hash, str) or not self.ir_hash.strip():
            raise ValueError(
                f"planned feature {self.column_name!r} has no ir_hash ({self.ir_hash!r}): §9 proves "
                f"completeness by comparing a staging manifest's ir_hash against the plan's, and a "
                f"blank one would compare nothing to nothing and pass")
        if not isinstance(self.physical_type, PhysicalType):
            raise TypeError(
                f"planned feature {self.column_name!r} needs a resolved PhysicalType, got "
                f"{type(self.physical_type).__name__}: `resolve_physical_type` returns either a type "
                f"or a governed refusal, and planning the refusal would publish a column whose type "
                f"§6 declined to decide")

    def identity_payload(self) -> dict[str, Any]:
        return {"column_name": self.column_name, "ir_hash": self.ir_hash,
                "physical_type": self.physical_type.identity_payload()}


@dataclass(frozen=True, slots=True)
class FeatureGroupPlanV1:
    """The packing list: which columns the group publishes, and under which contract.

    ``features`` is ordered by column name, so the plan of a group does not depend on the order its
    features happened to be compiled in. ``physical_type_policy_version`` is stored rather than read
    from the module at hash time, so a persisted plan states the rule its types were decided under.
    """

    logical_group_name: str
    materialization_contract_hash: str
    entity_key_columns: tuple[str, ...]
    business_dt_column: str
    features: tuple[PlannedFeature, ...]
    physical_type_policy_version: int

    def identity_payload(self) -> dict[str, Any]:
        """What the group PUBLISHES — no run id, no generation, no business date (§3.3).

        Deliberately TOTAL: no parsing, no catalog read, nothing that can raise. It is called while
        building a hash, where an exception would be indistinguishable from a governed refusal.
        """
        return {
            "logical_group_name": self.logical_group_name,
            "materialization_contract_hash": self.materialization_contract_hash,
            "entity_key_columns": list(self.entity_key_columns),
            "business_dt_column": self.business_dt_column,
            "features": [feature.identity_payload() for feature in self.features],
            "system_columns": list(SYSTEM_COLUMNS),
            "physical_type_policy_version": self.physical_type_policy_version,
        }


def group_plan_hash(plan: FeatureGroupPlanV1) -> str:
    """The plan's identity — ``materialize_hash`` is the package's one hasher (§14)."""
    return materialize_hash(plan.identity_payload())


@dataclass(frozen=True, slots=True)
class StagingManifestV1:
    """§9's per-feature evidence, written by the staging node and read at assembly.

    Every field is either identity (``ir_hash``, the generation/run/date binding, the two hashes) or
    an operator-facing count/location. No data value appears here, and none may be added: §14's rule
    for manifests is counts, types, hashes and locations only.
    """

    intent_feature_name: str
    ir_hash: str
    generation_id: str
    run_id: str
    business_dt: str
    generated_project_hash: str
    sandbox_execution_hash: str
    output_location: str
    schema_hash: str
    row_count: int
    status: StagingStatus

    def __post_init__(self) -> None:
        # Coerced, not merely annotated: the rendered pipeline writes JSON, so a manifest read back
        # carries the plain string and must canonicalize to the enum rather than becoming a second
        # spelling nothing compares equal to.
        object.__setattr__(self, "status", StagingStatus(self.status))
        if not isinstance(self.row_count, int) or isinstance(self.row_count, bool) \
                or self.row_count < 0:
            raise ValueError(
                f"staging manifest for {self.intent_feature_name!r} reports row_count "
                f"{self.row_count!r}: a staged output has a whole, non-negative number of rows, and "
                f"a negative or non-integer count is a manifest writer assembled wrongly")


@dataclass(frozen=True, slots=True)
class GateFailure:
    """One §9 verdict: a CLOSED code plus operator-facing detail (counts, hashes, names)."""

    code: ValidationGateCode
    detail: str


# ── building the plan ────────────────────────────────────────────────────────────────────────────


def key_columns_from_refs(ordered_keys: Sequence[str]) -> tuple[str, ...]:
    """The landing key's COLUMN names, from a contract's ordered key refs.

    The refs are the spine's (``hdfc::public.customers.cif_id``); the published table holds columns.
    They are normalized through the one Hive normalizer, so a catalog column that cannot be a Hive
    identifier is a plan error rather than a table that cannot be created.

    Takes the REFS rather than a contract so S6's V2 plan uses this normalizer instead of a second
    one — two spellings of "the published row is keyed by columns" is how a V2 group ends up keyed
    differently from the V1 group beside it.
    """
    columns: list[str] = []
    for ref in ordered_keys:
        try:
            _source, _schema, _table, column = parse_ref(ref)
        except ValueError as exc:
            raise ValueError(
                f"the contract's landing key {ref!r} is not an addressable column ref ({exc}): the "
                f"published row is keyed by columns, and a key nothing can name is not a key"
            ) from exc
        if column is None:
            raise ValueError(
                f"the contract's landing key {ref!r} names a TABLE, not a column: the published row "
                f"is one per (keys…, business_dt), so every key must be a column of it")
        columns.append(hive_identifier(column))
    return tuple(columns)


def _reject_collisions(columns: Sequence[tuple[str, ColumnRole]]) -> None:
    """Two columns may not share one name (spec §1.2: *never a silent overwrite*)."""
    seen: dict[str, ColumnRole] = {}
    for name, role in columns:
        clash = seen.get(name)
        if clash is not None:
            raise FeatureNamePlanError(
                f"the published row would carry {name!r} twice — once as a {clash.value} and once "
                f"as a {role.value}; one would silently overwrite the other")
        seen[name] = role


def build_group_plan(
    group: ContractGroup,
    features: Sequence[PlannedFeature],
    *,
    logical_group_name: str,
) -> FeatureGroupPlanV1:
    """The packing list for ``group``, or a plan error (§6, §9, §10.2).

    ``features`` must describe EXACTLY the group's members — the same closure Task 8.1 drew around
    operand evidence. A feature omitted from the plan is a feature the group publishes nothing for,
    and one nobody derived a contract for is a column with no contract behind it.

    Raises:
        TypeError: ``group`` is not a :class:`~featuregen.materialize.contract.ContractGroup`.
            §5.1's grouping is what proves the members share one contract, and planning a bare list
            of features would publish columns that were never shown to agree.
        ValueError: ``features`` does not describe exactly ``group.feature_names``, or a landing key
            ref does not name a column.
        FeatureNamePlanError: a name (the group's, a key's or a feature's) does not normalize to a
            Hive identifier, or two published columns normalize to one.
    """
    if not isinstance(group, ContractGroup):
        raise TypeError(
            f"build_group_plan requires a ContractGroup, got {type(group).__name__}: §5.1's "
            f"grouping is what establishes that these features share ONE materialization contract, "
            f"and a plan built without it would publish columns nobody showed to agree")

    name = hive_identifier(logical_group_name)
    planned = tuple(sorted(features, key=lambda feature: feature.column_name))
    expected = set(group.feature_names)
    supplied = {feature.column_name for feature in planned}
    if len(supplied) != len(planned):
        # Reported before the set comparison: two features that normalize to one column would
        # otherwise be described as "one missing feature", which names the wrong defect.
        _reject_collisions([(feature.column_name, ColumnRole.FEATURE) for feature in planned])
    if supplied != expected:
        raise ValueError(
            f"the planned features must describe exactly the group's members: missing "
            f"{sorted(expected - supplied)}, unexpected {sorted(supplied - expected)}. A member "
            f"with no planned column publishes nothing, and a planned column with no member is a "
            f"column no materialization contract covers")

    keys = key_columns_from_refs(group.contract.ordered_keys)
    business_dt = hive_identifier(group.contract.pit_semantics.business_dt_column)
    _reject_collisions([
        *[(key, ColumnRole.ENTITY_KEY) for key in keys],
        (business_dt, ColumnRole.BUSINESS_DT),
        *[(feature.column_name, ColumnRole.FEATURE) for feature in planned],
        # Included although a feature name can never begin with `_`: the guarantee is the
        # normalizer's, and asserting it here keeps the plan correct if that ever changes.
        *[(system, ColumnRole.SYSTEM) for system in SYSTEM_COLUMNS],
    ])

    return FeatureGroupPlanV1(
        logical_group_name=name,
        materialization_contract_hash=group.contract_hash,
        entity_key_columns=keys,
        business_dt_column=business_dt,
        features=planned,
        physical_type_policy_version=PHYSICAL_TYPE_POLICY_VERSION)


def expected_schema(plan: FeatureGroupPlanV1) -> tuple[PlannedColumn, ...]:
    """The published row, in the plan's declared order (§9, §10.2).

    Landing keys, then ``business_dt``, then the feature columns in name order, then the three
    system columns. The order is the PLAN's, not an engine's: a Hive ``DESCRIBE`` reports partition
    columns last, so a comparison against a live table matches names to types rather than
    positions — which is what §9's "required columns present, none extra" asks for anyway.
    """
    return (
        *[PlannedColumn(name=key, role=ColumnRole.ENTITY_KEY, sql_type=None, nullable=False)
          for key in plan.entity_key_columns],
        PlannedColumn(name=plan.business_dt_column, role=ColumnRole.BUSINESS_DT, sql_type=None,
                      nullable=False),
        *[PlannedColumn(name=feature.column_name, role=ColumnRole.FEATURE,
                        sql_type=feature.physical_type.sql_type,
                        nullable=feature.physical_type.nullable)
          for feature in plan.features],
        *[PlannedColumn(name=system, role=ColumnRole.SYSTEM, sql_type=SYSTEM_COLUMN_SQL_TYPE,
                        nullable=False)
          for system in SYSTEM_COLUMNS],
    )


def expected_schema_hash(plan: FeatureGroupPlanV1) -> str:
    """The schema identity §9 compares the assembled output against."""
    return materialize_hash(
        {"columns": [column.identity_payload() for column in expected_schema(plan)]})


# ── §9's completeness gate ───────────────────────────────────────────────────────────────────────


def _require(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"check_completeness needs a {name} to bind the manifests to ({value!r}): without it "
            f"every manifest would count as 'bound to this run' and STALE_STAGING_MANIFEST — the "
            f"whole reason a manifest carries a generation, a run and a date — would be unreachable")
    return value


def check_completeness(
    plan: FeatureGroupPlanV1,
    manifests: Sequence[StagingManifestV1],
    *,
    generation_id: str,
    run_id: str,
    business_dt: str,
) -> tuple[GateFailure, ...]:
    """§9 — is there exactly one completed, matching, THIS-run manifest per planned feature?

    Returns every failure, not the first: a gate that stopped early would send an operator round the
    regenerate/rerun loop once per broken feature. An empty tuple means the group may proceed.

    The order of the checks is the order of the questions:

    1. **Duplicates**, over the whole staging area — two manifests for one name mean two writes
       reached it, and grading either would be choosing which computation published.
    2. **Staleness**, over EVERY manifest present, planned or not — a manifest from another
       generation/run/date is the reused-path hazard itself, and it is judged before the ``ir_hash``
       because a manifest about another run is not evidence about this one.
    3. **Per planned feature** — present, ``completed``, and ``ir_hash``-matching. A ``failed``
       manifest is ``INCOMPLETE_COMPUTATION`` rather than a missing one: the record exists and says
       the column was never computed.
    4. **Unexpected manifests** bound to this run — output for a feature this plan does not contain,
       which would be an extra column at assembly.

    ``generated_project_hash`` and ``sandbox_execution_hash`` are deliberately NOT compared here.
    They have their own §9 gates (``PROJECT_INTEGRITY`` against ``GENERATED.lock``, and the run
    parameters), and checking them twice under a staging code would report one failure with two
    different names.
    """
    _require(generation_id, "generation_id")
    _require(run_id, "run_id")
    _require(business_dt, "business_dt")

    failures: list[GateFailure] = []
    by_name: dict[str, list[StagingManifestV1]] = {}
    for manifest in manifests:
        by_name.setdefault(manifest.intent_feature_name, []).append(manifest)

    duplicated = {name for name, found in by_name.items() if len(found) > 1}
    for name in sorted(duplicated):
        failures.append(GateFailure(
            ValidationGateCode.DUPLICATE_STAGING_MANIFEST,
            f"{len(by_name[name])} staging manifests name {name!r}; §9 requires exactly one per "
            f"planned feature, and choosing between them would choose which computation published"))

    expected_binding = (generation_id, run_id, business_dt)
    stale = {
        name for name, found in by_name.items()
        if name not in duplicated
        and (found[0].generation_id, found[0].run_id, found[0].business_dt) != expected_binding
    }
    for name in sorted(stale):
        manifest = by_name[name][0]
        failures.append(GateFailure(
            ValidationGateCode.STALE_STAGING_MANIFEST,
            f"the staging manifest for {name!r} is bound to generation "
            f"{manifest.generation_id!r}/run {manifest.run_id!r}/business_dt "
            f"{manifest.business_dt!r}, not to {generation_id!r}/{run_id!r}/{business_dt!r}: "
            f"staging paths are generation-scoped, so an older successful manifest whose ir_hash "
            f"still matches would publish stale output past every other check"))

    planned = {feature.column_name for feature in plan.features}
    for feature in plan.features:
        name = feature.column_name
        if name in duplicated or name in stale:
            continue
        found = by_name.get(name)
        if not found:
            failures.append(GateFailure(
                ValidationGateCode.MISSING_STAGING_MANIFEST,
                f"no staging manifest for planned feature {name!r}: completeness is proven by "
                f"manifest, not by schema — a column of the right shape proves nothing about which "
                f"computation produced it"))
            continue
        manifest = found[0]
        if manifest.status is not StagingStatus.COMPLETED:
            failures.append(GateFailure(
                ValidationGateCode.INCOMPLETE_COMPUTATION,
                f"the staging manifest for {name!r} records status {manifest.status.value!r}: the "
                f"staging job reported its own failure, so the column was never computed"))
            continue
        if manifest.ir_hash != feature.ir_hash:
            failures.append(GateFailure(
                ValidationGateCode.IR_HASH_MISMATCH,
                f"the staging manifest for {name!r} carries ir_hash {manifest.ir_hash!r}, and the "
                f"plan expects {feature.ir_hash!r}: the output was produced by a different "
                f"computation, which no schema check could ever reveal"))

    for name in sorted(set(by_name) - planned - duplicated - stale):
        failures.append(GateFailure(
            ValidationGateCode.UNEXPECTED_COLUMN,
            f"the staging area holds a manifest for {name!r}, which this group plan does not "
            f"contain: assembling it would add a column no materialization contract covers, and it "
            f"is evidence the staging path holds another plan's output"))
    return tuple(failures)
