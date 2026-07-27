"""Spec §5 — ``MaterializationContractV1``: derived PER FEATURE, then grouped by equal hash.

**Per feature, and then grouped (§5.1).** Deriving one contract from the UNION of the supplied IRs
would let a caller force a public feature into a restricted group merely by passing them together:
the group would take the maximum of everything and nobody would be told. So :func:`derive_contract`
takes ONE compiled IR and classifies ITS OWN §1.3 read set, and :func:`group_by_contract` groups the
results afterwards. This slice publishes exactly one group — more than one distinct hash returns
``MULTIPLE_MATERIALIZATION_CONTRACTS`` *listing the groups*, so an operator can see which features
disagreed and about what. Nothing is silently promoted.

**What a contract IS.** The meaning of the LANDING KEY: for the row ``(cif_id, business_dt)``, does
every column of that row mean the same thing? Entity and ordered keys, the landing point-in-time
semantics, the classification, retention, availability class, cadence, publication policy, backfill
boundary and the population declaration — plus the three POLICY VERSIONS under which those answers
were computed.

**What a contract is NOT, and both mistakes are easy.**

* The **calculation window is not identity (§5.3)**. A 30-day and a 90-day trailing feature share a
  contract. The window is *how a column was computed*, it lives in the expression IR, and putting it
  here would split a group per window and make the "one group" of this slice unreachable.
* The **resolved physical type is not identity** either — only ``PHYSICAL_TYPE_POLICY_VERSION`` is
  (§5.5). A ``BIGINT`` count and a ``DECIMAL(38,6)`` sum belong in one table; a contract carrying the
  type would give every differently typed feature its own contract, and the group would refuse
  itself. The per-column type belongs to ``FeatureGroupPlanV1`` (§6), which is per feature by design.

**The spine contributes ``identity_payload()`` only, never its provenance (§4).** Two people making
the identical semantic declaration must produce the same contract; letting ``declared_by`` in would
partition groups by who happened to declare the population.

**Two declared constants rather than an invented model.** No governed per-column retention exists in
this repository (it is a named deferred NFR), so ``DEFAULT_RETENTION_CLASS`` and
``RETENTION_POLICY_VERSION`` are declared here and both enter identity. That is the honest shape: the
alternative is to invent a retention model and hash it as if it were governed. When a real one
arrives, the policy version changes and every contract derived under it is a different artifact —
which is exactly the behaviour a retention change should have.

**Where §14 has no code, and what this module does instead.** A cadence naming a trigger this slice
does not implement, a timezone that is not a zone, a cutoff that is not a time, or an override that
LOOSENS the derived classification are all malformed DECLARATIONS — the caller has not yet said
anything the platform could refuse on governance grounds. §14's closed vocabulary has no member for
them, so they raise ``ValueError`` at the declaration boundary rather than borrowing a governed code
that means something else. The one thing that IS a governed verdict — a ``prohibited`` input, however
it was reached — returns ``MaterializationRefused(PROHIBITED_INPUT)`` exactly as §5.2 requires.
"""
from __future__ import annotations

import datetime as _datetime
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from featuregen.contracts.db import DbConn
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.classify import Classification, classify_read_set
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.ir import (
    AuthorizedCompilation,
    FormulaExecutionIRV1,
    physical_read_set,
)
from featuregen.materialize.physical_types import PHYSICAL_TYPE_POLICY_VERSION
from featuregen.materialize.spine import SpineSourceDeclarationV1
from featuregen.overlay.safety_floor import SENSITIVITY_ORDER, apply_sensitivity_floor

__all__ = [
    "BUSINESS_DT_COLUMN",
    "DEFAULT_RETENTION_CLASS",
    "RETENTION_POLICY_VERSION",
    "AvailabilityClass",
    "BackfillBoundary",
    "CadenceDecl",
    "CadencePeriod",
    "CadenceTrigger",
    "ContractGroup",
    "ContractOverrides",
    "LandingPitSemantics",
    "MaterializationContractV1",
    "PublicationPolicy",
    "contract_hash",
    "derive_contract",
    "derive_group_contract",
    "group_by_contract",
]

#: The version of the RETENTION rule below. It enters the contract hash (§5.5) so that replacing the
#: platform default with a governed per-column retention is a change of artifact identity.
RETENTION_POLICY_VERSION = 1

#: The retention class every contract in this slice carries. Named for what it is — a platform
#: default — rather than for a period nobody governed, so it cannot be mistaken for a governed fact.
DEFAULT_RETENTION_CLASS = "platform_default"

#: The landing partition column. One row per ``(keys…, business_dt)`` is the published shape all
#: through §8/§9/§10, and the name is part of what the landing key MEANS.
BUSINESS_DT_COLUMN = "business_dt"

#: The rank that refuses materialization — addressed by RANK, never by a duplicated literal, for the
#: reason ``classify`` gives: a scale that grows a level must move the refusal with it.
_REFUSING_RESTRICTION = SENSITIVITY_ORDER[-1]


class CadencePeriod(StrEnum):
    """How often the group is materialized. CLOSED, and one member in this slice — "one entity, one
    cadence (daily)" is a stated first-slice bound, and an open string here would let an hourly
    cadence be declared against machinery that has no hourly ``business_dt``."""

    DAILY = "daily"


class CadenceTrigger(StrEnum):
    """What STARTS a run. ``dependencies_ready`` is deliberately absent: it is a named deferred NFR,
    and accepting it would schedule runs against a dependency graph this slice does not have."""

    SCHEDULED = "scheduled"
    MANUAL = "manual"


class AvailabilityClass(StrEnum):
    """The DECLARED publication promise for the group (§5.4).

    Declared, not derived: deriving it needs a governed source-delivery SLA, which is a named
    deferred NFR. Recorded as a closed vocabulary so the promise is comparable between contracts
    rather than free text nobody can group by.
    """

    #: Published on the business date it describes.
    SAME_DAY = "same_day"
    #: Published on the calendar day after the business date it describes.
    NEXT_DAY = "next_day"
    #: No declared deadline — the honest answer where no delivery commitment exists.
    BEST_EFFORT = "best_effort"


class PublicationPolicy(StrEnum):
    """How the group's columns become visible. ``atomic_group`` is §5.4's default and §10's whole
    invariant: a reader sees the complete previous partition or the complete new one."""

    ATOMIC_GROUP = "atomic_group"


class BackfillBoundary(StrEnum):
    """What a backfill re-computes. ``group_level`` is §5.4's default: the group is the unit of
    publication, so it is also the unit of re-publication."""

    GROUP_LEVEL = "group_level"


@dataclass(frozen=True, slots=True)
class CadenceDecl:
    """The declared schedule, validated at the declaration boundary.

    ``business_date_cutoff`` is a wall-clock time IN ``timezone`` — together they derive the cutoff
    §8 rule 1 applies to every availability column. It is canonicalized on construction so that
    ``"00:00"`` and ``"00:00:00"`` are one contract rather than two: a spelling difference must never
    fork a group.
    """

    period: CadencePeriod
    timezone: str
    business_date_cutoff: str
    trigger: CadenceTrigger

    def __post_init__(self) -> None:
        if not isinstance(self.period, CadencePeriod):
            raise ValueError(
                f"cadence period must be one of {[p.value for p in CadencePeriod]}, got "
                f"{self.period!r}: this slice is bounded to one cadence, and a period it cannot "
                f"schedule would be a declaration nothing honours")
        if not isinstance(self.trigger, CadenceTrigger):
            raise ValueError(
                f"cadence trigger must be one of {[t.value for t in CadenceTrigger]}, got "
                f"{self.trigger!r}. 'dependencies_ready' is a DEFERRED NFR: there is no dependency "
                f"graph in this slice for a run to wait on, so accepting it would declare a trigger "
                f"nothing can fire")
        try:
            ZoneInfo(self.timezone)
        # Deliberately NOT a bare `Exception`: `ZoneInfoNotFoundError` is a `KeyError`, a malformed
        # key is a `ValueError` and a non-string is a `TypeError` — all of them are the same
        # DECLARATION bug. An `OSError` (an unreadable tz database) is an environment failure and
        # must not be reported to a declarer as "your zone is wrong".
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(
                f"cadence timezone {self.timezone!r} is not a known IANA zone ({exc}): the cutoff is "
                f"a wall-clock time in a named zone, so an unknown zone makes business_dt mean "
                f"nothing") from exc
        object.__setattr__(self, "business_date_cutoff", _canonical_cutoff(
            self.business_date_cutoff))

    def identity_payload(self) -> dict[str, Any]:
        return {"period": self.period.value, "timezone": self.timezone,
                "business_date_cutoff": self.business_date_cutoff, "trigger": self.trigger.value}


def _canonical_cutoff(value: str) -> str:
    """``HH:MM[:SS]`` → the canonical ``HH:MM:SS``, or a declaration error.

    A cutoff carrying its own UTC offset is refused rather than reconciled: the zone is declared once
    in ``timezone``, and a second answer to "which clock?" is how two contracts that look identical
    apply two different cutoffs.
    """
    try:
        parsed = _datetime.time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"cadence business_date_cutoff {value!r} is not a wall-clock time of day (expected "
            f"HH:MM or HH:MM:SS): {exc}") from exc
    if parsed.tzinfo is not None:
        raise ValueError(
            f"cadence business_date_cutoff {value!r} carries its own UTC offset: the zone is "
            f"declared once, in the cadence's timezone, and a cutoff that carries a second one "
            f"would silently apply a different clock than the contract says it does")
    return parsed.isoformat()


@dataclass(frozen=True, slots=True)
class ContractOverrides:
    """A declared TIGHTENING of what the catalog said (§5.4) — monotonic in both fields.

    ``sensitivity_class`` may only RAISE, by the shipped rule that governs every other sensitivity
    decision in this platform (``safety_floor``: evidence may only raise; going below a floor needs a
    governed ``SafetyOverride``, which is not a materialization concern). ``access_requirements`` may
    only ADD.

    Retention and cadence are deliberately NOT overridable. Ordering retention classes to decide
    which way is "stricter" would require the retention model §5.2 explicitly says not to invent, and
    a cadence override is a different declaration rather than a tightening of this one.
    """

    sensitivity_class: str | None = None
    access_requirements: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.sensitivity_class is not None and self.sensitivity_class not in SENSITIVITY_ORDER:
            raise ValueError(
                f"override sensitivity_class {self.sensitivity_class!r} is not a rank of the "
                f"governed scale {list(SENSITIVITY_ORDER)}: an unrankable CATALOG value is "
                f"normalized and then refused, but an unrankable DECLARED value is a typo, and "
                f"normalizing it to the top of the scale would turn a misspelling into a prohibition")
        object.__setattr__(self, "access_requirements", tuple(self.access_requirements))


@dataclass(frozen=True, slots=True)
class LandingPitSemantics:
    """What ``(keys…, business_dt)`` MEANS — §5.3's question, and nothing about how a column was
    computed.

    ``availability_basis_class`` is the distinct set of governed availability bases the feature's
    expressions are gated on. Two rows keyed alike whose columns are gated on ``posted_at`` in one
    case and ``ingested_at`` in another do not mean the same thing, so they are not one contract. The
    LAG is deliberately absent: the gate is ``availability <= cutoff`` whatever the lag's magnitude,
    so the lag changes which rows qualify (an expression-IR fact, hashed there) and not what the
    landing key claims.
    """

    entity_keys: tuple[str, ...]
    business_dt_column: str
    cutoff_timezone: str
    cutoff_time: str
    availability_basis_class: tuple[str, ...]

    def identity_payload(self) -> dict[str, Any]:
        return {"entity_keys": list(self.entity_keys),
                "business_dt_column": self.business_dt_column,
                "cutoff_timezone": self.cutoff_timezone,
                "cutoff_time": self.cutoff_time,
                "availability_basis_class": list(self.availability_basis_class)}


@dataclass(frozen=True, slots=True)
class MaterializationContractV1:
    """One feature's materialization contract — the group key of §5.

    It carries NO feature name, no IR hash and no physical type: those are per-feature facts, and a
    contract that held any of them could never be shared, which would make the group of §10 a group
    of one.

    ``spine`` is the whole declaration (so the contract is readable on its own), but only its
    :meth:`~featuregen.materialize.spine.SpineSourceDeclarationV1.identity_payload` enters identity.
    """

    entity: str
    ordered_keys: tuple[str, ...]
    pit_semantics: LandingPitSemantics
    sensitivity_class: str
    access_requirements: tuple[str, ...]
    retention_class: str
    retention_policy_version: int
    availability_class: AvailabilityClass
    cadence: CadenceDecl
    publication_policy: PublicationPolicy
    backfill_boundary: BackfillBoundary
    spine: SpineSourceDeclarationV1
    classification_policy_version: int
    physical_type_policy_version: int

    def identity_payload(self) -> dict[str, Any]:
        """§5.5's INCLUDE list, exactly — no run id, no watermark, no wall-clock, no provenance.

        Deliberately TOTAL: no parsing, no catalog read, nothing that can raise. It is called while
        building a hash, where an exception would be indistinguishable from a governed refusal.
        """
        return {
            "entity": self.entity,
            "ordered_keys": list(self.ordered_keys),
            "pit_semantics": self.pit_semantics.identity_payload(),
            "sensitivity_class": self.sensitivity_class,
            "access_requirements": list(self.access_requirements),
            "retention_class": self.retention_class,
            "retention_policy_version": self.retention_policy_version,
            "availability_class": self.availability_class.value,
            "cadence": self.cadence.identity_payload(),
            "publication_policy": self.publication_policy.value,
            "backfill_boundary": self.backfill_boundary.value,
            "spine": self.spine.identity_payload(),
            "classification_policy_version": self.classification_policy_version,
            "physical_type_policy_version": self.physical_type_policy_version,
        }


def contract_hash(contract: MaterializationContractV1) -> str:
    """The group key — ``materialize_hash`` is the package's one hasher (§14)."""
    return materialize_hash(contract.identity_payload())


@dataclass(frozen=True, slots=True)
class ContractGroup:
    """The single contract a compilation publishes under, and every feature that shares it."""

    contract_hash: str
    contract: MaterializationContractV1
    feature_names: tuple[str, ...]


def derive_contract(
    conn: DbConn,
    ir: FormulaExecutionIRV1,
    *,
    cadence: CadenceDecl,
    availability_class: AvailabilityClass,
    overrides: ContractOverrides | None = None,
) -> MaterializationContractV1 | MaterializationRefused:
    """Derive ONE feature's contract from its OWN read set (§5.1), or refuse it.

    The read set is §1.3's complete union for this feature — every expression read, every join
    endpoint, every availability column and the spine's own columns — taken from
    :func:`~featuregen.materialize.ir.physical_read_set` rather than re-walked here, so the
    classification cannot be computed over a narrower set than the one Gate 2 authorized.

    Returns:
        The contract, or a :class:`MaterializationRefused` carrying ``PROHIBITED_INPUT`` when any
        element of this feature's read set classifies at the most restrictive rank — including when
        a declared override tightened it there.

    Raises:
        ValueError: ``overrides`` would LOOSEN the derived classification (§5.4 is monotonic), or the
            read set is empty / holds a ref the catalog cannot address. A declaration the caller has
            not yet made coherent is not a governed verdict, and §14 has no member for it.
    """
    classification = classify_read_set(conn, physical_read_set((ir,), ir.spine))
    if isinstance(classification, MaterializationRefused):
        return classification

    sensitivity_class, access_requirements = _tighten(classification, overrides)
    if sensitivity_class == _REFUSING_RESTRICTION:
        return MaterializationRefused(
            CompilationRefusalCode.PROHIBITED_INPUT,
            f"the declared override raises {ir.feature_name} to {_REFUSING_RESTRICTION!r}: "
            f"materialization is refused rather than published as a {_REFUSING_RESTRICTION!r} "
            f"feature group, exactly as it is when the catalog says so")

    spine = ir.spine
    return MaterializationContractV1(
        entity=spine.entity,
        # The LANDING keys: the published row is one per population key per business_dt. A feature's
        # own grain columns may be a different table's spelling of the same entity (`compile_ir`
        # refuses a different entity outright), and they are identity-bearing in the IR, not here.
        ordered_keys=tuple(spine.ordered_key_refs),
        pit_semantics=LandingPitSemantics(
            entity_keys=tuple(spine.ordered_key_refs),
            business_dt_column=BUSINESS_DT_COLUMN,
            cutoff_timezone=cadence.timezone,
            cutoff_time=cadence.business_date_cutoff,
            availability_basis_class=tuple(sorted(
                {expression.pit.availability_basis.value for expression in ir.expressions}))),
        sensitivity_class=sensitivity_class,
        access_requirements=access_requirements,
        retention_class=DEFAULT_RETENTION_CLASS,
        retention_policy_version=RETENTION_POLICY_VERSION,
        availability_class=availability_class,
        cadence=cadence,
        publication_policy=PublicationPolicy.ATOMIC_GROUP,
        backfill_boundary=BackfillBoundary.GROUP_LEVEL,
        spine=spine.declaration,
        classification_policy_version=classification.classification_policy_version,
        physical_type_policy_version=PHYSICAL_TYPE_POLICY_VERSION)


def _tighten(
    classification: Classification, overrides: ContractOverrides | None
) -> tuple[str, tuple[str, ...]]:
    """Apply a declared override MONOTONICALLY, or raise (§5.4).

    The raise-only rule is the shipped one: ``apply_sensitivity_floor`` returns the maximum of the
    derived class and the declared one, so an override that comes back as something other than
    itself is an override that tried to go DOWN.
    """
    if overrides is None:
        return classification.sensitivity_class, classification.access_requirements

    sensitivity_class = classification.sensitivity_class
    if overrides.sensitivity_class is not None:
        raised = apply_sensitivity_floor(sensitivity_class, [overrides.sensitivity_class])
        if raised != overrides.sensitivity_class:
            raise ValueError(
                f"override sensitivity_class {overrides.sensitivity_class!r} is looser than the "
                f"derived class {sensitivity_class!r}: overrides are monotonic, so a contract may "
                f"be tightened but never relaxed. Lowering a governed classification is a "
                f"SafetyOverride decision for privacy or security, not a materialization request")
        sensitivity_class = raised

    requirements = set(classification.access_requirements)
    if overrides.access_requirements:
        dropped = requirements - set(overrides.access_requirements)
        if dropped:
            raise ValueError(
                f"override access_requirements drop {sorted(dropped)}, which the read set requires: "
                f"overrides are monotonic, and a looser access requirement would publish a column "
                f"to readers the catalog says may not see its inputs")
        requirements |= set(overrides.access_requirements)
    return sensitivity_class, tuple(sorted(requirements))


def group_by_contract(
    contracts: Mapping[str, MaterializationContractV1]
) -> ContractGroup | MaterializationRefused:
    """Group features by EQUAL contract hash (§5.1), or refuse a compilation that has more than one.

    ``contracts`` is keyed by feature name — the contract itself carries none, because a contract
    that identified its feature could never be shared.

    Returns:
        The single :class:`ContractGroup`, or a :class:`MaterializationRefused` carrying
        ``MULTIPLE_MATERIALIZATION_CONTRACTS`` whose detail LISTS each group with its features. It
        lists rather than merges on purpose: merging is the promotion §5.1 exists to prevent, and an
        operator needs to see which features disagreed before deciding which group is the right one.

    Raises:
        ValueError: ``contracts`` is empty — a group over no features is a publication target for
            nothing.
    """
    if not contracts:
        raise ValueError(
            "group_by_contract was called with no features: a group over nothing is a publication "
            "target nobody can publish to, and the next stage cannot tell it apart from a group "
            "whose features all agreed")

    grouped: dict[str, list[str]] = {}
    by_hash: dict[str, MaterializationContractV1] = {}
    for feature_name, contract in contracts.items():
        digest = contract_hash(contract)
        grouped.setdefault(digest, []).append(feature_name)
        by_hash.setdefault(digest, contract)

    if len(grouped) > 1:
        described = "; ".join(
            f"{digest}: {', '.join(sorted(names))}" for digest, names in sorted(grouped.items()))
        return MaterializationRefused(
            CompilationRefusalCode.MULTIPLE_MATERIALIZATION_CONTRACTS,
            f"{len(contracts)} feature(s) derived {len(grouped)} distinct materialization "
            f"contracts, and this slice publishes exactly one group — {described}. The groups are "
            f"listed rather than merged: merging would promote the least restrictive contract into "
            f"the most restrictive one and publish features under a contract nobody declared for "
            f"them")

    digest, names = next(iter(grouped.items()))
    return ContractGroup(contract_hash=digest, contract=by_hash[digest],
                         feature_names=tuple(sorted(names)))


def derive_group_contract(
    conn: DbConn,
    authorization: AuthorizedCompilation,
    *,
    cadence: CadenceDecl,
    availability_class: AvailabilityClass,
    overrides: ContractOverrides | None = None,
) -> ContractGroup | MaterializationRefused:
    """Derive every feature's contract and group them — the only entry point to a publishable group.

    It takes the Gate 2 TOKEN rather than a sequence of IRs, which is what keeps §1.3's "a refused
    group produces no contract, group plan or project" true by construction: a refusal is not an
    :class:`~featuregen.materialize.ir.AuthorizedCompilation`, so it cannot be passed here.

    The per-feature derivation is still per feature — this function maps :func:`derive_contract` over
    the authorized IRs and never over their union.

    Raises:
        TypeError: ``authorization`` is not an ``AuthorizedCompilation``. Waving an unauthorized
            group past the gate is a call assembled wrongly, not a governed verdict.
    """
    if not isinstance(authorization, AuthorizedCompilation):
        raise TypeError(
            f"derive_group_contract requires an AuthorizedCompilation (Gate 2's token), got "
            f"{type(authorization).__name__}: §1.3 authorizes the group's complete read set as one "
            f"decision, and a contract derived without it would be a contract over reads nobody "
            f"authorized")

    contracts: dict[str, MaterializationContractV1] = {}
    for ir in authorization.irs:
        derived = derive_contract(conn, ir, cadence=cadence,
                                  availability_class=availability_class, overrides=overrides)
        if isinstance(derived, MaterializationRefused):
            return derived
        contracts[ir.feature_name] = derived
    return group_by_contract(contracts)
