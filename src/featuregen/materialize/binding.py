"""Spec §10.1 — the physical target is bound to its contract, in TWO append-only records.

``sandbox_feature.cif_daily`` is a human name; the materialization contract hash is the group key.
Nothing else stops a *different* contract — changed cutoff semantics, a different spine declaration,
sensitivity, retention or cadence — from overwriting that table and silently replacing one
materialization contract with a semantically different one under an unchanged name. That is what
``GROUP_BINDING_CONFLICT`` refuses.

**An append-only row cannot hold a field that moves.** The binding (logical name → contract hash →
physical target) is therefore written ONCE, every change to the packing list APPENDS a
:class:`GroupPlanRevision`, and *"current plan"* is **DERIVED** as the latest revision that
published successfully — never stored as a mutable field. Adding a feature appends a revision and
keeps the binding; only a different CONTRACT is a conflict.

**Publication success cannot live on the revision.** The revision is appended when the packing list
changes; whether that plan published is known only afterwards, from the append-only run events §12
folds. So :func:`current_plan_revision` takes the published generations as EVIDENCE. A revision
nobody published is not the current plan however recent it is — otherwise a failed publication
would silently become the group's definition.

**Pure by design (task ordering).** The tables these records live in are created with the rest of
the control plane, so nothing here touches a connection: these are functions over values, and their
persistence is proven where the tables exist. A binding that could only be checked by writing it
would be a check that runs after the damage.

**Why the two conflicts are typed differently.** A stored binding whose CONTRACT (or physical
target) differs is a governed publication verdict — the group compiled perfectly and must not
publish — so it is a ``MaterializationRefused`` carrying a ``PublicationRefusalCode``. A binding for
a *different logical name*, or a revision appended under another group's binding, is a LOOKUP bug at
the call site: the binding is fetched BY the logical name, so the wrong one is not a verdict about
the plan at all, and §14 has no member for it.
"""
from __future__ import annotations

import datetime as _datetime
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from featuregen.materialize.admission import hive_identifier
from featuregen.materialize.codes import MaterializationRefused, PublicationRefusalCode
from featuregen.materialize.group_plan import FeatureGroupPlanV1, group_plan_hash

__all__ = [
    "SANDBOX_NAMESPACE",
    "GroupContractBinding",
    "GroupPlanRevision",
    "bind_group",
    "current_plan_revision",
    "physical_target_for",
    "plan_revision",
]

#: §7's ONE namespace. There is no production path in this slice, and Child-2 must later supply a
#: factory that validates actual frozen bindings before one exists. Declared once here so the target
#: is DERIVED from the sandbox identity rather than accepted as a caller string (§10.3).
SANDBOX_NAMESPACE = "sandbox_feature"


def physical_target_for(logical_group_name: str) -> str:
    """The published table for a logical group name — derived, never supplied.

    Raises:
        featuregen.materialize.admission.FeatureNamePlanError: the name does not normalize to a Hive
            identifier, so no table could carry it.
    """
    return f"{SANDBOX_NAMESPACE}.{hive_identifier(logical_group_name)}"


@dataclass(frozen=True, slots=True)
class GroupContractBinding:
    """Written ONCE per logical name (§10.1). Every field is fixed at the moment of binding.

    There is deliberately no ``current_group_plan_hash``, no ``status`` and no ``updated_at``: the
    table is append-only (UPDATE, DELETE and TRUNCATE all blocked), so a field that moves could not
    be honoured, and the two-record split exists precisely so that nothing here has to.

    ``physical_target`` is *derived* from ``logical_group_name`` by :func:`physical_target_for`, but
    it is NOT re-derived in ``__post_init__``: a binding recorded under an earlier derivation must
    still be loadable, or :func:`bind_group` could never report the disagreement.
    """

    binding_id: str
    logical_group_name: str
    materialization_contract_hash: str
    physical_target: str


@dataclass(frozen=True, slots=True)
class GroupPlanRevision:
    """Appended whenever the packing list changes (§10.1) — a fact, not a state.

    ``created_at`` is validated as an OFFSET-AWARE ISO 8601 timestamp because "latest" must be an
    instant: two naive timestamps written in two zones are not orderable, and a lexicographic
    comparison of mixed offsets picks the wrong packing list outright
    (``2026-07-27T23:00:00+05:30`` is 17:30Z — earlier than ``2026-07-27T19:00:00+00:00``, and later
    as a string).
    """

    binding_id: str
    group_plan_hash: str
    generation_id: str
    created_at: str

    def __post_init__(self) -> None:
        for name in ("binding_id", "group_plan_hash", "generation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"group plan revision {name} is blank ({value!r}): a revision that names no "
                    f"binding, plan or generation records nothing that could be derived from")
        _instant(self.created_at)

    def instant(self) -> _datetime.datetime:
        """``created_at`` as the moment it denotes — the only ordering the derivation uses."""
        return _instant(self.created_at)


def _instant(created_at: str) -> _datetime.datetime:
    """Parse an offset-aware ISO 8601 timestamp, or say why it cannot be ordered."""
    try:
        parsed = _datetime.datetime.fromisoformat(created_at)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"group plan revision created_at {created_at!r} is not an ISO 8601 timestamp ({exc}): "
            f"the current plan is the LATEST published revision, so the record must name an "
            f"instant") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"group plan revision created_at {created_at!r} carries no UTC offset: two naive "
            f"timestamps written in two zones are not orderable, so 'latest' would be a guess")
    return parsed


def bind_group(
    plan: FeatureGroupPlanV1,
    *,
    binding_id: str,
    existing: GroupContractBinding | None = None,
) -> GroupContractBinding | MaterializationRefused:
    """Bind ``plan``'s logical name to its contract, or refuse to publish under someone else's.

    With no ``existing`` binding the name is bound for the first time. With one, the binding is
    RETURNED UNCHANGED when the contract still agrees — adding, removing or retyping features
    changes ``group_plan_hash`` and leaves the binding alone, which is the whole point of splitting
    the record in two.

    Args:
        plan: the packing list about to publish.
        binding_id: the id a FIRST binding is recorded under. Ignored when ``existing`` is supplied,
            because that binding was written once already and its id is part of what was written.
        existing: the binding already recorded for ``plan.logical_group_name``, if any.

    Returns:
        The binding to publish under, or a :class:`MaterializationRefused` carrying
        ``GROUP_BINDING_CONFLICT`` when the name already resolves to a different contract — or to a
        different physical table, which is the same silent replacement by another route.

    Raises:
        ValueError: ``binding_id`` is blank while binding for the first time, or ``existing``
            belongs to a different logical name (a lookup bug — the binding is fetched BY that name).
    """
    target = physical_target_for(plan.logical_group_name)
    if existing is None:
        if not isinstance(binding_id, str) or not binding_id.strip():
            raise ValueError(
                f"bind_group needs a binding_id to record a first binding for "
                f"{plan.logical_group_name!r} ({binding_id!r}): every plan revision is appended "
                f"against that id, and a blank one would orphan the group's whole history")
        return GroupContractBinding(
            binding_id=binding_id,
            logical_group_name=plan.logical_group_name,
            materialization_contract_hash=plan.materialization_contract_hash,
            physical_target=target)

    if existing.logical_group_name != plan.logical_group_name:
        raise ValueError(
            f"the supplied binding is for {existing.logical_group_name!r} while the plan publishes "
            f"{plan.logical_group_name!r}: a binding is looked up BY the logical name, so the wrong "
            f"one is a lookup bug rather than a verdict about this plan — and comparing the two "
            f"would report a conflict between groups that never shared a name")

    if existing.materialization_contract_hash != plan.materialization_contract_hash:
        return MaterializationRefused(
            PublicationRefusalCode.GROUP_BINDING_CONFLICT,
            f"{plan.logical_group_name!r} is bound to materialization contract "
            f"{existing.materialization_contract_hash} and this plan carries "
            f"{plan.materialization_contract_hash}: publishing would replace one materialization "
            f"contract with a semantically different one under an unchanged name, which is exactly "
            f"what a reader of {existing.physical_target} could not detect")
    if existing.physical_target != target:
        return MaterializationRefused(
            PublicationRefusalCode.GROUP_BINDING_CONFLICT,
            f"{plan.logical_group_name!r} is bound to the physical target "
            f"{existing.physical_target!r} while this plan would publish to {target!r}: the target "
            f"is derived from the logical name, so a stored one that disagrees means the name "
            f"already resolves to a table other than the one about to be written")
    return existing


def plan_revision(
    plan: FeatureGroupPlanV1,
    binding: GroupContractBinding,
    *,
    generation_id: str,
    created_at: str,
) -> GroupPlanRevision:
    """Append-ready: the revision recording that ``plan`` is what ``binding``'s group now packs.

    The plan's hash is COMPUTED here rather than accepted, so a revision cannot name a packing list
    that is not the one it was built from.

    Raises:
        ValueError: ``binding`` belongs to a different logical group (a lookup bug — appending one
            group's plan under another's binding would make that group's derived current plan a plan
            it never had), or ``created_at`` is not an offset-aware ISO 8601 timestamp.
    """
    if binding.logical_group_name != plan.logical_group_name:
        raise ValueError(
            f"the supplied binding is for {binding.logical_group_name!r} while the plan publishes "
            f"{plan.logical_group_name!r}: appending this revision would make "
            f"{binding.logical_group_name!r}'s derived current plan a plan it never had")
    return GroupPlanRevision(
        binding_id=binding.binding_id,
        group_plan_hash=group_plan_hash(plan),
        generation_id=generation_id,
        created_at=created_at)


def current_plan_revision(
    revisions: Sequence[GroupPlanRevision],
    *,
    published_generation_ids: Collection[str],
) -> GroupPlanRevision | None:
    """The group's CURRENT plan — derived from the records, never read from a field (§10.1).

    Args:
        revisions: every revision appended for ONE binding.
        published_generation_ids: the generations that published successfully, folded from the
            append-only run events (§12). Passed as evidence because success is not knowable when a
            revision is appended.

    Returns:
        The latest published revision, or ``None`` when nothing has published yet — a group whose
        first publication has not succeeded has no current plan, which is not the same as having an
        empty one.

    Raises:
        ValueError: the revisions belong to more than one binding (folding two groups' histories
            together derives a current plan for neither), or two DIFFERENT published revisions share
            the latest instant. The tie is refused rather than broken: publishing either would be an
            arbitrary choice about what the group contains.
    """
    supplied = tuple(revisions)
    if not supplied:
        return None

    bindings = {revision.binding_id for revision in supplied}
    if len(bindings) > 1:
        raise ValueError(
            f"current_plan_revision was given revisions from {len(bindings)} bindings "
            f"({', '.join(sorted(bindings))}): each binding is one logical group's history, and "
            f"folding two of them together derives a current plan for neither")

    published = [revision for revision in supplied
                 if revision.generation_id in published_generation_ids]
    if not published:
        return None

    latest = max(revision.instant() for revision in published)
    tied = [revision for revision in published if revision.instant() == latest]
    distinct = {(revision.group_plan_hash, revision.generation_id) for revision in tied}
    if len(distinct) > 1:
        raise ValueError(
            f"{len(distinct)} different published revisions share the latest instant "
            f"{latest.isoformat()}: the current plan is ambiguous, and picking one would be an "
            f"arbitrary choice about which columns the group contains")
    return tied[0]
