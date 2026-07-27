"""Spec A Task 10 — §10.1's group binding: two append-only records, and a DERIVED current plan.

`sandbox_feature.cif_daily` is a human name; the contract hash is the group key. Nothing else stops
a *different* contract — a changed cutoff, spine declaration, sensitivity, retention or cadence —
from overwriting that table and silently replacing one materialization contract with another under
an unchanged name. `GROUP_BINDING_CONFLICT` is the refusal that stops it.

**An append-only row cannot hold a field that moves.** So the binding (logical name → contract hash
→ physical target) is written ONCE, each change to the packing list appends a `GroupPlanRevision`,
and "current plan" is DERIVED as the latest revision that published successfully. The tests here
assert both halves: the binding's field set is pinned with `==` so no mutable field can be added
without failing, and `current_plan_revision` is a function over records rather than a field read.

**Publication success is not on the revision.** It cannot be: the row is appended when the plan
changes, and whether that plan published is known only afterwards, from the append-only run events
§12 folds. So the derivation takes the published generations as evidence, and a revision nobody
published is not the current plan however recent it is.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize.test_group_plan import (
    CADENCE,
    COUNT_90D,
    GROUP,
    RATIO_90D,
    SUM_30D,
    _contract,
    _feature,
    _group,
    _plan,
)

from featuregen.materialize.binding import (
    SANDBOX_NAMESPACE,
    GroupContractBinding,
    GroupPlanRevision,
    bind_group,
    current_plan_revision,
    physical_target_for,
    plan_revision,
)
from featuregen.materialize.codes import (
    CompilationRefusalCode,
    MaterializationRefused,
    PublicationRefusalCode,
)
from featuregen.materialize.group_plan import group_plan_hash

BINDING_ID = "bind-0001"
GEN_1 = "gen-0001"
GEN_2 = "gen-0002"
AT_1 = "2026-07-27T09:00:00+00:00"
AT_2 = "2026-07-28T09:00:00+00:00"


def _bound(plan=None, *, binding_id=BINDING_ID) -> GroupContractBinding:
    result = bind_group(plan if plan is not None else _plan(), binding_id=binding_id)
    assert isinstance(result, GroupContractBinding), result
    return result


# ══ §10.1 — the binding record has no mutable field ══════════════════════════════════════════════


def test_the_binding_record_has_NO_mutable_field() -> None:
    """Pinned with `==`, not `<=`: a superset assertion would let `current_group_plan_hash` (or a
    `status`, or an `updated_at`) be added tomorrow, and an append-only table cannot hold one."""
    assert {field.name for field in dataclasses.fields(GroupContractBinding)} == {
        "binding_id", "logical_group_name", "materialization_contract_hash", "physical_target"}


def test_the_revision_record_carries_no_outcome() -> None:
    """§10.1's field set exactly. A `published` flag here would be the mutable field the two-record
    split exists to remove — the outcome is known only after the run, from §12's events."""
    assert {field.name for field in dataclasses.fields(GroupPlanRevision)} == {
        "binding_id", "group_plan_hash", "generation_id", "created_at"}


def test_neither_record_can_be_assigned_to() -> None:
    binding = _bound()
    revision = plan_revision(_plan(), binding, generation_id=GEN_1, created_at=AT_1)
    for record, field in ((binding, "materialization_contract_hash"),
                          (revision, "group_plan_hash")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(record, field, "rewritten")


def test_the_physical_target_is_derived_from_the_SANDBOX_namespace() -> None:
    """§7: there is no production path, and the target is derived from the sandbox identity rather
    than accepted as a caller string."""
    assert _bound().physical_target == f"{SANDBOX_NAMESPACE}.{GROUP}"
    assert physical_target_for(GROUP) == "sandbox_feature.cif_daily"


def test_the_physical_target_NORMALIZES_the_group_name() -> None:
    """One normalizer, not two: a target built from an unnormalized name would point at a table
    the plan's own column names could never sit in."""
    assert physical_target_for("CIF_Daily") == "sandbox_feature.cif_daily"


def test_a_binding_needs_an_id() -> None:
    with pytest.raises(ValueError, match="binding_id"):
        bind_group(_plan(), binding_id="  ")


# ══ §10.1 — adding a feature keeps the binding ═══════════════════════════════════════════════════


def test_adding_a_feature_changes_the_plan_hash_and_KEEPS_the_binding() -> None:
    two = _plan()
    three = _plan(*two.features, _feature(RATIO_90D, ir_hash="ir-ratio"))
    binding = _bound(two)
    assert group_plan_hash(two) != group_plan_hash(three)
    assert bind_group(three, binding_id=BINDING_ID, existing=binding) is binding


def test_adding_a_feature_APPENDS_a_revision() -> None:
    two = _plan()
    three = _plan(*two.features, _feature(RATIO_90D, ir_hash="ir-ratio"))
    binding = _bound(two)
    first = plan_revision(two, binding, generation_id=GEN_1, created_at=AT_1)
    second = plan_revision(three, binding, generation_id=GEN_2, created_at=AT_2)
    assert first.binding_id == second.binding_id == binding.binding_id
    assert first.group_plan_hash != second.group_plan_hash


def test_a_revision_carries_THIS_plans_hash() -> None:
    plan = _plan()
    binding = _bound(plan)
    assert plan_revision(plan, binding, generation_id=GEN_1,
                         created_at=AT_1).group_plan_hash == group_plan_hash(plan)


def test_a_revision_may_not_leave_a_field_BLANK() -> None:
    """A revision naming no binding, plan or generation records nothing the derivation could read —
    and a blank generation could never be found among the published ones, so the group would have
    no current plan for a reason nobody could see."""
    binding = _bound()
    with pytest.raises(ValueError, match="generation_id"):
        plan_revision(_plan(), binding, generation_id="  ", created_at=AT_1)
    with pytest.raises(ValueError, match="binding_id"):
        GroupPlanRevision(binding_id="", group_plan_hash="plan-1", generation_id=GEN_1,
                          created_at=AT_1)
    with pytest.raises(ValueError, match="group_plan_hash"):
        GroupPlanRevision(binding_id=BINDING_ID, group_plan_hash=" ", generation_id=GEN_1,
                          created_at=AT_1)


def test_a_revision_against_ANOTHER_groups_binding_is_a_caller_error() -> None:
    """Appending one group's plan under another's binding would make that group's derived current
    plan a plan it never had."""
    other = dataclasses.replace(_bound(), logical_group_name="cif_monthly",
                                physical_target=physical_target_for("cif_monthly"))
    with pytest.raises(ValueError, match="cif_monthly"):
        plan_revision(_plan(), other, generation_id=GEN_1, created_at=AT_1)


# ══ §10.1 — a different contract under one name is a PUBLICATION refusal ═════════════════════════


def test_a_DIFFERENT_contract_hash_for_the_same_logical_name_CONFLICTS() -> None:
    here = _plan()
    elsewhere_contract = _contract(cadence=dataclasses.replace(CADENCE, timezone="Europe/London"))
    there = _plan(*here.features,
                  group=_group(SUM_30D, COUNT_90D, contract=elsewhere_contract))
    binding = _bound(here)
    refusal = bind_group(there, binding_id=BINDING_ID, existing=binding)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.GROUP_BINDING_CONFLICT


def test_the_conflict_is_a_PUBLICATION_refusal_not_a_compilation_one() -> None:
    """§14 keeps the two vocabularies apart: the group compiled perfectly and must not publish."""
    here = _plan()
    there = _plan(*here.features, group=_group(
        SUM_30D, COUNT_90D,
        contract=_contract(cadence=dataclasses.replace(CADENCE, timezone="Europe/London"))))
    refusal = bind_group(there, binding_id=BINDING_ID, existing=_bound(here))
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code not in set(CompilationRefusalCode)


def test_a_binding_recorded_against_another_PHYSICAL_TARGET_conflicts() -> None:
    """The target is derived from the logical name, so a stored one that disagrees means the name
    already resolves to a table other than the one this plan would publish."""
    binding = dataclasses.replace(_bound(), physical_target="other_ns.cif_daily")
    refusal = bind_group(_plan(), binding_id=BINDING_ID, existing=binding)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.GROUP_BINDING_CONFLICT


def test_an_unchanged_contract_returns_the_SAME_binding_object() -> None:
    binding = _bound()
    assert bind_group(_plan(), binding_id="bind-9999", existing=binding) is binding


def test_a_binding_for_a_DIFFERENT_logical_name_is_a_caller_error() -> None:
    """The binding is looked up BY the logical name, so the wrong one is a lookup bug — not a
    governed verdict about the plan."""
    other = dataclasses.replace(_bound(), logical_group_name="cif_monthly",
                                physical_target=physical_target_for("cif_monthly"))
    with pytest.raises(ValueError, match="cif_monthly"):
        bind_group(_plan(), binding_id=BINDING_ID, existing=other)


# ══ §10.1 — "current plan" is DERIVED, never stored ══════════════════════════════════════════════


def _revisions(binding, *pairs) -> tuple[GroupPlanRevision, ...]:
    return tuple(dataclasses.replace(
        plan_revision(_plan(), binding, generation_id=generation, created_at=at),
        group_plan_hash=f"plan-{generation}") for generation, at in pairs)


def test_the_current_plan_is_the_LATEST_SUCCESSFULLY_PUBLISHED_revision() -> None:
    binding = _bound()
    first, second = _revisions(binding, (GEN_1, AT_1), (GEN_2, AT_2))
    current = current_plan_revision((first, second), published_generation_ids={GEN_1, GEN_2})
    assert current is second


def test_a_revision_that_never_published_is_NOT_the_current_plan() -> None:
    """The newest packing list is not the published one until a run says so — otherwise a failed
    publication would silently become the group's definition."""
    binding = _bound()
    first, second = _revisions(binding, (GEN_1, AT_1), (GEN_2, AT_2))
    assert current_plan_revision((first, second), published_generation_ids={GEN_1}) is first


def test_no_published_revision_yields_NONE() -> None:
    binding = _bound()
    revisions = _revisions(binding, (GEN_1, AT_1))
    assert current_plan_revision(revisions, published_generation_ids=set()) is None


def test_no_revisions_at_all_yields_NONE() -> None:
    assert current_plan_revision((), published_generation_ids={GEN_1}) is None


def test_the_latest_is_by_INSTANT_not_by_the_TEXT_of_the_timestamp() -> None:
    """`2026-07-27T23:00:00+05:30` is 17:30Z — EARLIER than `2026-07-27T19:00:00+00:00`, and later
    than it as a string. Lexicographic ordering would pick the wrong packing list."""
    binding = _bound()
    dubai, utc = _revisions(binding, (GEN_1, "2026-07-27T23:00:00+05:30"),
                            (GEN_2, "2026-07-27T19:00:00+00:00"))
    assert current_plan_revision((dubai, utc), published_generation_ids={GEN_1, GEN_2}) is utc


def test_a_naive_timestamp_is_refused() -> None:
    """Two naive timestamps from two zones are not orderable, so 'latest' would be a guess."""
    with pytest.raises(ValueError, match="offset"):
        plan_revision(_plan(), _bound(), generation_id=GEN_1, created_at="2026-07-27T09:00:00")


def test_a_timestamp_that_is_not_ISO_8601_is_refused() -> None:
    with pytest.raises(ValueError):
        plan_revision(_plan(), _bound(), generation_id=GEN_1, created_at="27 July 2026")


def test_revisions_from_TWO_bindings_are_a_caller_error() -> None:
    """Folding two groups' histories together would derive a current plan for neither."""
    binding = _bound()
    other = dataclasses.replace(binding, binding_id="bind-0002")
    mine = _revisions(binding, (GEN_1, AT_1))
    theirs = _revisions(other, (GEN_2, AT_2))
    with pytest.raises(ValueError, match="binding"):
        current_plan_revision((*mine, *theirs), published_generation_ids={GEN_1, GEN_2})


def test_an_AMBIGUOUS_tie_refuses_rather_than_picking_one() -> None:
    """Two published revisions at the same instant name two packing lists; publishing either would
    be an arbitrary choice about what the group contains."""
    binding = _bound()
    left, right = _revisions(binding, (GEN_1, AT_1), (GEN_2, AT_1))
    with pytest.raises(ValueError, match="ambiguous"):
        current_plan_revision((left, right), published_generation_ids={GEN_1, GEN_2})


def test_TWO_PLANS_recorded_under_one_generation_are_ambiguous() -> None:
    """One generation compiles one packing list, so two plan hashes under it is a corrupted
    history — and picking either would publish a plan that generation never produced. The tie is
    judged on `(plan hash, generation)`, not on the generation alone, for exactly this case."""
    binding = _bound()
    (left,) = _revisions(binding, (GEN_1, AT_1))
    right = dataclasses.replace(left, group_plan_hash="plan-something-else")
    with pytest.raises(ValueError, match="ambiguous"):
        current_plan_revision((left, right), published_generation_ids={GEN_1})


def test_the_SAME_revision_recorded_twice_is_not_ambiguous() -> None:
    binding = _bound()
    (only,) = _revisions(binding, (GEN_1, AT_1))
    assert current_plan_revision((only, only), published_generation_ids={GEN_1}) is only


def test_the_derivation_reads_no_field_named_for_the_current_plan() -> None:
    """The invariant stated structurally: nothing on either record answers 'which plan is live'."""
    names = {field.name for field in dataclasses.fields(GroupContractBinding)} | {
        field.name for field in dataclasses.fields(GroupPlanRevision)}
    assert not [name for name in names if "current" in name or "latest" in name]
