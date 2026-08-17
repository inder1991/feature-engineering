"""S6 — the V2 contract and its group: classified over the read set that INCLUDES policy columns.

**Why this is not just V1's function with a different return type.** §5.2 classifies sensitivity over
"what this feature reads", and since C-C4 a V2 feature's read set is the union of its expression
reads, its spine reads AND the columns its governed policies read. A contract derived from the
structural half alone would classify a feature that joins a restricted FX rate table as though it
never touched it — and Gate 2 would have authorized the read, so nothing downstream would catch it.
:meth:`~featuregen.materialize.boundary_v2.PlannedFormulaExecutionIRV2.read_set` is that union
already, so this derivation takes the PLANNED IR and never re-walks anything.

**Everything else is the shipped rule, imported rather than restated.** ``classify_read_set``,
``tighten_classification``'s monotonic override and ``override_availability_promise`` are V1's, so
"a contract may be tightened but never relaxed" has one implementation. The only V2-shaped field is
``physical_type_policy``: V1 carries an ordinal counter, V2 names the rule set a type was decided
under.

**Grouping refuses rather than merges,** for V1's reason: merging is the promotion §5.1 exists to
prevent, and an operator needs to see which features disagreed before deciding which group is right.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.boundary_v2 import (
    MaterializationContractV2,
    PlannedFormulaExecutionIRV2,
    contract_hash_v2,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.contract import (
    BUSINESS_DT_COLUMN,
    DEFAULT_RETENTION_CLASS,
    RETENTION_POLICY_VERSION,
    AvailabilityPromiseV1,
    BackfillBoundary,
    CadenceDecl,
    ContractOverrides,
    LandingPitSemantics,
    PublicationPolicy,
    classify_read_set,
    override_availability_promise,
    tighten_classification,
)
from featuregen.overlay.upload.field_policies import SENSITIVITY_ORDER

__all__ = [
    "ContractGroupV2",
    "derive_contract_v2",
    "group_by_contract_v2",
]

_REFUSING_RESTRICTION = SENSITIVITY_ORDER[-1]


@dataclass(frozen=True, slots=True)
class ContractGroupV2:
    """The single contract a V2 compilation publishes under, and every feature that shares it."""

    contract_hash: str
    contract: MaterializationContractV2
    feature_names: tuple[str, ...]


def derive_contract_v2(
    conn: DbConn,
    planned: PlannedFormulaExecutionIRV2,
    *,
    cadence: CadenceDecl,
    availability_promise: AvailabilityPromiseV1,
    physical_type_policy: str,
    overrides: ContractOverrides | None = None,
) -> MaterializationContractV2 | MaterializationRefused:
    """Derive ONE V2 feature's contract from its PLANNED read set, or refuse it.

    Takes the planned IR rather than the bare one so the classification runs over the exact union
    Gate 2 decides — policy columns included. A derivation given the bare IR could only reach the
    structural half, and the narrower answer is the one the sensitivity class would be computed
    from.

    Returns:
        The contract, or a :class:`MaterializationRefused` carrying ``PROHIBITED_INPUT`` when any
        element of the read set classifies at the most restrictive rank — including when a declared
        override tightened it there.

    Raises:
        ValueError: ``overrides`` would LOOSEN the derived classification or move the availability
            promise EARLIER (§5.4 is monotonic in both), or ``physical_type_policy`` is not a policy
            id. All three are declarations the caller has not made coherent, and §14 has no member
            for that.
    """
    promise = availability_promise
    if not isinstance(promise, AvailabilityPromiseV1):
        raise ValueError(
            f"availability_promise must be an AvailabilityPromiseV1, got {type(promise).__name__} "
            f"({promise!r}): §5.6 replaced the invented class label with a canonical value, and a "
            f"label would enter the contract hash as an uncomparable string")
    if overrides is not None and overrides.availability_promise is not None:
        promise = override_availability_promise(
            promise, overrides.availability_promise,
            current_cadence=cadence, proposed_cadence=cadence)

    # THE union — expression, spine and policy reads — not a re-walk of the structural half.
    classification = classify_read_set(conn, planned.read_set)
    if isinstance(classification, MaterializationRefused):
        return classification

    sensitivity_class, access_requirements = tighten_classification(classification, overrides)
    if sensitivity_class == _REFUSING_RESTRICTION:
        return MaterializationRefused(
            CompilationRefusalCode.PROHIBITED_INPUT,
            f"the declared override raises {planned.ir.feature_name} to "
            f"{_REFUSING_RESTRICTION!r}: materialization is refused rather than published as a "
            f"{_REFUSING_RESTRICTION!r} feature group, exactly as it is when the catalog says so")

    spine = planned.ir.spine
    return MaterializationContractV2(
        entity=spine.entity,
        ordered_keys=tuple(spine.ordered_key_refs),
        pit_semantics=LandingPitSemantics(
            entity_keys=tuple(spine.ordered_key_refs),
            business_dt_column=BUSINESS_DT_COLUMN,
            cutoff_timezone=cadence.timezone,
            cutoff_time=cadence.business_date_cutoff,
            availability_basis_class=tuple(sorted(
                {expression.pit.availability_basis.value
                 for expression in planned.ir.expressions}))),
        sensitivity_class=sensitivity_class,
        access_requirements=access_requirements,
        retention_class=DEFAULT_RETENTION_CLASS,
        retention_policy_version=RETENTION_POLICY_VERSION,
        availability_promise=promise,
        cadence=cadence,
        publication_policy=PublicationPolicy.ATOMIC_GROUP,
        backfill_boundary=BackfillBoundary.GROUP_LEVEL,
        spine=spine.declaration,
        classification_policy_version=classification.classification_policy_version,
        physical_type_policy=physical_type_policy)


def group_by_contract_v2(
    contracts: Mapping[str, MaterializationContractV2]
) -> ContractGroupV2 | MaterializationRefused:
    """Group V2 features by EQUAL contract hash (§5.1), or refuse a compilation with more than one.

    Returns:
        The single :class:`ContractGroupV2`, or a :class:`MaterializationRefused` carrying
        ``MULTIPLE_MATERIALIZATION_CONTRACTS`` whose detail LISTS each group with its features. It
        lists rather than merges: merging is the promotion §5.1 exists to prevent, and an operator
        needs to see which features disagreed before choosing.

    Raises:
        ValueError: no features. A group of nothing publishes nothing, and a contract hash for it
            would be the same hash for every empty compilation.
    """
    if not contracts:
        raise ValueError(
            "group_by_contract_v2 was given no features: a group of nothing publishes nothing, and "
            "grouping it would produce one contract hash shared by every empty compilation")

    by_hash: dict[str, list[str]] = {}
    contract_of: dict[str, MaterializationContractV2] = {}
    for feature_name, contract in contracts.items():
        digest = contract_hash_v2(contract)
        by_hash.setdefault(digest, []).append(feature_name)
        contract_of[digest] = contract

    if len(by_hash) > 1:
        described = "; ".join(
            f"{digest}: {', '.join(sorted(names))}"
            for digest, names in sorted(by_hash.items()))
        return MaterializationRefused(
            CompilationRefusalCode.MULTIPLE_MATERIALIZATION_CONTRACTS,
            f"the compilation derives {len(by_hash)} materialization contracts across "
            f"{len(contracts)} feature(s): {described}. §5.1 publishes a group under ONE contract, "
            f"and merging them would promote every member to the strictest — which is the "
            f"promotion the grouping exists to prevent, so the disagreement is reported instead")

    digest, names = next(iter(by_hash.items()))
    return ContractGroupV2(contract_hash=digest, contract=contract_of[digest],
                           feature_names=tuple(sorted(names)))


def contracts_for(
    conn: DbConn,
    planned: Sequence[PlannedFormulaExecutionIRV2],
    *,
    cadence: CadenceDecl,
    availability_promise: AvailabilityPromiseV1,
    physical_type_policy: str,
    overrides: ContractOverrides | None = None,
) -> dict[str, MaterializationContractV2] | MaterializationRefused:
    """Every member's contract, or the FIRST refusal.

    Stops at the first refusal rather than collecting them, because a refused member refuses the
    whole compilation — a group is published as one thing — and continuing would derive contracts
    nobody can use.
    """
    derived: dict[str, MaterializationContractV2] = {}
    for member in planned:
        contract = derive_contract_v2(
            conn, member, cadence=cadence, availability_promise=availability_promise,
            physical_type_policy=physical_type_policy, overrides=overrides)
        if isinstance(contract, MaterializationRefused):
            return contract
        derived[member.ir.feature_name] = contract
    return derived
