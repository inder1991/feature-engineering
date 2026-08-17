"""S6 — the V2 group plan, and MEMBERSHIP as something the platform can be asked about.

**The plan is V1's, with one V2-shaped field.** Column normalization, collision rejection and the
landing-key derivation are imported, not restated: two spellings of "the published row is keyed by
columns" is how a V2 group ends up keyed differently from the V1 group beside it. The V2 difference
is ``physical_type_policy`` — a named rule set rather than V1's ordinal counter — and it is STORED on
the plan so a persisted artifact states what its types were decided under.

**Membership is queryable, which is S6's fourth acceptance clause and not a convenience.** Until now
"which features does this group publish" and "which group publishes this feature" could only be
answered by re-deriving the compilation — and a question you answer by re-deriving is a question you
answer differently once the inputs move. The rows are written from the PLAN, so what is queryable is
what was planned rather than what someone believed was planned.

**Environment-scoped, per F3.** A group's key is at least ``(environment_id, logical_group_name)``:
environment is deployment placement, not feature meaning, so it must not fold into the group name —
and it must appear in every query, uniqueness constraint and membership row, or two environments
publishing the same logical group silently share one membership.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.boundary_v2 import FeatureGroupPlanV2, group_plan_hash_v2
from featuregen.materialize.contract_v2 import ContractGroupV2
from featuregen.materialize.group_plan import (
    SYSTEM_COLUMNS,
    ColumnRole,
    PlannedFeature,
    _reject_collisions,
    hive_identifier,
    key_columns_from_refs,
)

__all__ = [
    "GroupMembershipV2",
    "build_group_plan_v2",
    "group_of_feature",
    "members_of_group",
    "record_group_plan",
]


def build_group_plan_v2(
    group: ContractGroupV2,
    features: Sequence[PlannedFeature],
    *,
    logical_group_name: str,
    physical_type_policy: str,
) -> FeatureGroupPlanV2:
    """The packing list for a V2 ``group`` — the same rules as V1's, over the V2 contract.

    Raises:
        TypeError: ``group`` is not a :class:`ContractGroupV2`. §5.1's grouping is what proves the
            members share one contract, and planning a bare list of features would publish columns
            that were never shown to agree.
        ValueError: ``features`` does not describe exactly ``group.feature_names``. A member with no
            planned column publishes nothing, and a planned column with no member is a column no
            materialization contract covers.
        FeatureNamePlanError: two published columns normalize to one name.
    """
    if not isinstance(group, ContractGroupV2):
        raise TypeError(
            f"build_group_plan_v2 requires a ContractGroupV2, got {type(group).__name__}: §5.1's "
            f"grouping is what establishes that these features share ONE materialization contract, "
            f"and a plan built without it would publish columns nobody showed to agree")

    name = hive_identifier(logical_group_name)
    planned = tuple(sorted(features, key=lambda feature: feature.column_name))
    expected = set(group.feature_names)
    supplied = {feature.column_name for feature in planned}
    if len(supplied) != len(planned):
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
        *[(system, ColumnRole.SYSTEM) for system in SYSTEM_COLUMNS],
    ])

    return FeatureGroupPlanV2(
        logical_group_name=name,
        materialization_contract_hash=group.contract_hash,
        entity_key_columns=keys,
        business_dt_column=business_dt,
        features=planned,
        physical_type_policy=physical_type_policy)


# ── membership, persisted and asked about ───────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class GroupMembershipV2:
    """One published column's membership: which group, in which environment, from which feature."""

    environment_id: str
    logical_group_name: str
    group_plan_hash: str
    materialization_contract_hash: str
    column_name: str
    #: The V2 IR that produces this column — read from the plan, so membership names the exact
    #: compiled plan behind a published column rather than only the column's name.
    ir_hash: str


def record_group_plan(
    conn: DbConn, plan: FeatureGroupPlanV2, *, environment_id: str,
) -> str:
    """Append a group plan and its membership rows. Returns the plan hash.

    The IR hash on each membership row is read from the PLAN's own ``PlannedFeature``, never taken
    as a second argument: ``PlannedFeature`` already refuses a blank one, and a caller-supplied map
    would be a second statement about which compiled plan produces a column — free to disagree with
    the plan that is being recorded.

    Raises:
        ValueError: no environment. Environment is deployment placement, so two environments
            publishing the same logical group would otherwise share one membership.
    """
    if not environment_id.strip():
        raise ValueError(
            "a group plan must name the environment it publishes in: environment is deployment "
            "placement, and two environments publishing the same logical group would otherwise "
            "share one membership")

    plan_hash = group_plan_hash_v2(plan)
    conn.execute(
        "INSERT INTO materialization_group_v2 (group_plan_hash, environment_id, "
        "logical_group_name, materialization_contract_hash, entity_key_columns, "
        "business_dt_column, physical_type_policy) VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (group_plan_hash) DO NOTHING",
        (plan_hash, environment_id, plan.logical_group_name,
         plan.materialization_contract_hash, list(plan.entity_key_columns),
         plan.business_dt_column, plan.physical_type_policy))
    for feature in plan.features:
        conn.execute(
            "INSERT INTO materialization_group_member (group_plan_hash, column_name, ir_hash) "
            "VALUES (%s, %s, %s) ON CONFLICT (group_plan_hash, column_name) DO NOTHING",
            (plan_hash, feature.column_name, feature.ir_hash))
    return plan_hash


def members_of_group(
    conn: DbConn, *, environment_id: str, logical_group_name: str,
) -> tuple[GroupMembershipV2, ...]:
    """Which columns a group publishes, in an environment — the forward question.

    Keyed on the pair, never on the name alone: the same logical group in two environments is two
    groups, and answering across them would report columns a caller cannot read.
    """
    return tuple(
        GroupMembershipV2(
            environment_id=row[0], logical_group_name=row[1], group_plan_hash=row[2],
            materialization_contract_hash=row[3], column_name=row[4], ir_hash=row[5])
        for row in conn.execute(
            "SELECT g.environment_id, g.logical_group_name, g.group_plan_hash, "
            "g.materialization_contract_hash, m.column_name, m.ir_hash "
            "FROM materialization_group_v2 g "
            "JOIN materialization_group_member m ON m.group_plan_hash = g.group_plan_hash "
            "WHERE g.environment_id = %s AND g.logical_group_name = %s "
            "ORDER BY m.column_name", (environment_id, logical_group_name)).fetchall())


def group_of_feature(
    conn: DbConn, *, environment_id: str, feature_name: str,
) -> tuple[GroupMembershipV2, ...]:
    """Which groups publish a feature, in an environment — the reverse question.

    ``feature_name`` is the PUBLISHED COLUMN name, which is what a feature is called in a plan: V1's
    ``build_group_plan`` already compares ``group.feature_names`` against the planned column names,
    so the two are one vocabulary and a second column holding the same string would be a field that
    can only ever agree or be wrong.

    A tuple rather than one row: a feature may be published by more than one group plan over time,
    and collapsing that to "the group" would answer a question about history as though it were a
    question about now.
    """
    return tuple(
        GroupMembershipV2(
            environment_id=row[0], logical_group_name=row[1], group_plan_hash=row[2],
            materialization_contract_hash=row[3], column_name=row[4], ir_hash=row[5])
        for row in conn.execute(
            "SELECT g.environment_id, g.logical_group_name, g.group_plan_hash, "
            "g.materialization_contract_hash, m.column_name, m.ir_hash "
            "FROM materialization_group_v2 g "
            "JOIN materialization_group_member m ON m.group_plan_hash = g.group_plan_hash "
            "WHERE g.environment_id = %s AND m.column_name = %s "
            "ORDER BY g.logical_group_name, g.group_plan_hash",
            (environment_id, feature_name)).fetchall())
