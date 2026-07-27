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
semantics, the classification, retention, the availability PROMISE, cadence, publication policy,
backfill boundary and the population declaration — plus the three POLICY VERSIONS under which those
answers were computed.

**The availability promise is a VALUE, not a label (§5.6).** An earlier draft declared a closed
``AvailabilityClass`` vocabulary — ``same_day`` / ``next_day`` / ``best_effort`` — which entered the
contract hash and so made an arbitrary member list load-bearing: anyone needing ``T+0`` or ``T+5``
had to add a member, and adding one re-keyed every group that already existed.
:class:`AvailabilityPromiseV1` replaces it with a structured offset that expresses all of them
without a schema change, and carries its ``kind`` discriminator FROM v1 so that the day a second kind
(``BUSINESS_DAY_OFFSET``) arrives, existing payloads stay byte-identical and nothing re-keys.

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
    "AvailabilityPromiseKind",
    "AvailabilityPromiseV1",
    "BackfillBoundary",
    "CadenceDecl",
    "CadencePeriod",
    "CadenceTrigger",
    "ContractGroup",
    "ContractOverrides",
    "LandingPitSemantics",
    "MaterializationContractV1",
    "PromiseComparison",
    "PublicationPolicy",
    "compare_availability_promises",
    "contract_hash",
    "derive_contract",
    "derive_group_contract",
    "group_by_contract",
    "override_availability_promise",
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


class AvailabilityPromiseKind(StrEnum):
    """What a promise's ``calendar_days`` COUNTS — the discriminator, carried from v1 (§5.6).

    One member today, and shipping it anyway is the whole forward-compatibility property: adding
    ``BUSINESS_DAY_OFFSET`` later must leave every existing canonical payload byte-identical, and it
    cannot if v1 omitted the field and v2 added it.

    **``T+N`` means CALENDAR days in v1, explicitly.** A banking-day promise is a different kind, and
    it additionally requires a governed holiday-calendar identifier and version before it can mean
    anything — the two must never be silently read as each other, which is precisely why the offset
    names its basis instead of leaving it to whoever reads the number.
    """

    CALENDAR_OFFSET = "calendar_offset"


class PromiseComparison(StrEnum):
    """Where one promise stands relative to another — and the fourth answer that is not a place.

    ``INCOMPARABLE`` is a VERDICT, not the absence of ``LATER``. Two promises counted from different
    cutoffs (or, once a second kind exists, on different day bases) are not ordered at all, and
    collapsing that into "not later" would let a monotonic override be decided by a comparison that
    means nothing.
    """

    EARLIER = "earlier"
    SAME = "same"
    LATER = "later"
    INCOMPARABLE = "incomparable"


#: CALENDAR_OFFSET's arithmetic: the width of the day ``calendar_days`` counts, and therefore both
#: the canonical bound on ``plus_minutes`` and the carry the normalizing constructor applies. A
#: second kind must state its own — a banking day is not 1440 minutes after the previous one.
_MINUTES_PER_CALENDAR_DAY = 1440


@dataclass(frozen=True, slots=True)
class AvailabilityPromiseV1:
    """The DECLARED publication promise for the group (§5.4, §5.6) — a canonical value.

    Its meaning is fixed::

        business_dt at CadenceDecl.business_date_cutoff
          + calendar_days   (in CadenceDecl.timezone)
          + plus_minutes

    so ``T+3 plus two hours`` is ``AvailabilityPromiseV1(calendar_days=3, plus_minutes=120)``.
    Declared and not derived: deriving it needs a governed source-delivery SLA, a named deferred NFR.

    **Non-canonical input is REFUSED here, and normalized somewhere else.** ``(calendar_days=0,
    plus_minutes=1560)`` is not a value; it is an input, and :meth:`normalized` is the only thing that
    turns it into one. Normalizing silently inside this constructor would make two spellings of one
    promise both "work" and leave no call site able to show which was meant — and group equality
    would then compare spellings rather than promises.

    **Minutes, not hours**, so ``T+1 plus 30 minutes`` needs no schema change.

    The bound ``0 <= plus_minutes < 1440`` is also what makes ``(calendar_days, plus_minutes)``
    orderable as a plain tuple: once minutes may reach a whole day, ``(1, 0)`` and ``(0, 1440)`` are
    the same instant and the tuple comparison deciding a monotonic override starts lying.
    """

    kind: AvailabilityPromiseKind = AvailabilityPromiseKind.CALENDAR_OFFSET
    calendar_days: int = 0
    plus_minutes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AvailabilityPromiseKind):
            raise ValueError(
                f"availability promise kind must be one of "
                f"{[k.value for k in AvailabilityPromiseKind]}, got {self.kind!r}: the kind is the "
                f"discriminator that enters the contract hash, and one nothing can interpret would "
                f"put an unreadable claim into identity")
        _whole_number(self.calendar_days, "calendar_days")
        _whole_number(self.plus_minutes, "plus_minutes")
        if self.calendar_days < 0:
            raise ValueError(
                f"availability promise calendar_days must be >= 0, got {self.calendar_days}: a "
                f"promise cannot land before the business date it describes")
        if not 0 <= self.plus_minutes < _MINUTES_PER_CALENDAR_DAY:
            raise ValueError(
                f"availability promise plus_minutes must satisfy 0 <= plus_minutes < "
                f"{_MINUTES_PER_CALENDAR_DAY}, got {self.plus_minutes}: this is the CANONICAL form, "
                f"and a whole day spelled as minutes is a second spelling of a promise that already "
                f"has one. Use AvailabilityPromiseV1.normalized(...) to canonicalize an offset")

    @classmethod
    def normalized(
        cls,
        *,
        kind: AvailabilityPromiseKind = AvailabilityPromiseKind.CALENDAR_OFFSET,
        calendar_days: int = 0,
        plus_minutes: int = 0,
    ) -> AvailabilityPromiseV1:
        """The SEPARATE normalizing constructor (§5.6): any spelling of one offset → the one form.

        ``(0, 1560)`` becomes ``(1, 120)``, and ``(1, -30)`` becomes ``(0, 1410)`` — a re-spelling of
        a single total, never a second semantics. It is a distinct entry point on purpose: a caller
        that MEANT the canonical form uses the plain constructor and is told when it is wrong, while
        a caller normalizing arithmetic it computed says so at the call site.

        Raises:
            ValueError: the total offset is negative (no promise lands before the business date), or
                a component is not a whole number of days / minutes.
        """
        # BEFORE the arithmetic, not after: `"3" * 1440` is a 1440-character string, and the
        # `TypeError` that eventually follows would report a declaration mistake as a crash.
        for name, value in (("calendar_days", calendar_days), ("plus_minutes", plus_minutes)):
            _whole_number(value, name)
        total = calendar_days * _MINUTES_PER_CALENDAR_DAY + plus_minutes
        if total < 0:
            raise ValueError(
                f"availability promise offset {calendar_days}d {plus_minutes}m totals {total} "
                f"minutes: a promise cannot land before the business date it describes, so there is "
                f"no canonical form to normalize it to")
        days, minutes = divmod(total, _MINUTES_PER_CALENDAR_DAY)
        return cls(kind=kind, calendar_days=days, plus_minutes=minutes)

    def identity_payload(self) -> dict[str, Any]:
        """The canonical payload that enters the contract hash — DECLARED values only.

        ``kind`` is present from v1 (see :class:`AvailabilityPromiseKind`). Nothing observed is here:
        when the data actually landed is a run-time fact, and a promise carrying one would change the
        group's identity on every run without a single declaration changing.
        """
        return {"kind": self.kind.value, "calendar_days": self.calendar_days,
                "plus_minutes": self.plus_minutes}


def _whole_number(value: object, name: str) -> None:
    """A declared offset component must be a plain ``int`` — it is hashed.

    ``bool`` is rejected although it is an ``int``: ``calendar_days=True`` is a call assembled
    wrongly, not a one-day promise. A ``str`` is rejected because ``"3"`` and ``3`` are different
    canonical JSON and would silently be two groups.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(
            f"availability promise {name} must be an int, got {type(value).__name__} ({value!r}): "
            f"the promise is hashed into the contract, so a differently typed spelling of the same "
            f"number would be a different group")


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


def _comparison_basis(
    promise: AvailabilityPromiseV1, cadence: CadenceDecl
) -> tuple[str, str, str]:
    """What must MATCH before two promises can be ordered at all (§5.6).

    A promise is an offset from a cutoff, so the cutoff is half of what it means: "T+3 at 23:59
    Asia/Dubai" and "T+3 at 18:00 UTC" are different clocks and the same three digits. The ``kind``
    is here for the same reason one day ahead of time — a calendar-day offset and a banking-day
    offset count different things, so neither is "later" than the other.
    """
    return (promise.kind.value, cadence.timezone, cadence.business_date_cutoff)


def compare_availability_promises(
    left: AvailabilityPromiseV1,
    right: AvailabilityPromiseV1,
    *,
    left_cadence: CadenceDecl,
    right_cadence: CadenceDecl,
) -> PromiseComparison:
    """Where ``right`` stands relative to ``left`` — or that the question does not apply.

    Returns ``INCOMPARABLE`` whenever the two promises are counted from different clocks or on
    different day bases (:func:`_comparison_basis`), and only then compares
    ``(calendar_days, plus_minutes)``. That tuple comparison is exact BECAUSE the canonical form
    bounds minutes below a day; it would be wrong for un-normalized input, which is why
    :class:`AvailabilityPromiseV1` refuses it.
    """
    if _comparison_basis(left, left_cadence) != _comparison_basis(right, right_cadence):
        return PromiseComparison.INCOMPARABLE
    lhs = (left.calendar_days, left.plus_minutes)
    rhs = (right.calendar_days, right.plus_minutes)
    if rhs > lhs:
        return PromiseComparison.LATER
    if rhs < lhs:
        return PromiseComparison.EARLIER
    return PromiseComparison.SAME


def override_availability_promise(
    current: AvailabilityPromiseV1,
    proposed: AvailabilityPromiseV1,
    *,
    current_cadence: CadenceDecl,
    proposed_cadence: CadenceDecl,
) -> AvailabilityPromiseV1:
    """Apply a MONOTONIC promise override (§5.4): later accepted, earlier refused, unequal clocks
    refused.

    Later (or the same) is accepted because a promise is a commitment to consumers and relaxing one
    is a declaration the platform can honour. Earlier is refused because it claims data will be
    available sooner than what was derived — a claim nothing backs.

    Raises:
        ValueError: ``proposed`` is EARLIER than ``current``, or the two are INCOMPARABLE. Both are
            caller errors, not governed refusals: §14's codes describe valid requests rejected by the
            catalog or by data state, and neither of these is a verdict about governed facts. The two
            messages are kept apart because an incomparable pair was never ordered — reporting it as
            "earlier" would assert a comparison that was not made.
    """
    verdict = compare_availability_promises(
        current, proposed, left_cadence=current_cadence, right_cadence=proposed_cadence)
    if verdict is PromiseComparison.INCOMPARABLE:
        raise ValueError(
            f"availability promise override {proposed.identity_payload()} is incomparable with "
            f"{current.identity_payload()}: they are counted from "
            f"{_comparison_basis(current, current_cadence)} and "
            f"{_comparison_basis(proposed, proposed_cadence)}, which are different clocks or "
            f"different day bases. The override is refused rather than ordered — an ordering "
            f"imposed on two promises that were never comparable would let a monotonic check pass "
            f"on a comparison that means nothing")
    if verdict is PromiseComparison.EARLIER:
        raise ValueError(
            f"availability promise override {proposed.identity_payload()} is earlier than the "
            f"declared {current.identity_payload()}: overrides are monotonic (§5.4), so a promise "
            f"may be made later but never earlier. Promising sooner than what was declared is a "
            f"commitment nothing in the platform backs")
    return proposed


@dataclass(frozen=True, slots=True)
class ContractOverrides:
    """A declared TIGHTENING of what the catalog said (§5.4) — monotonic in both fields.

    ``sensitivity_class`` may only RAISE, by the shipped rule that governs every other sensitivity
    decision in this platform (``safety_floor``: evidence may only raise; going below a floor needs a
    governed ``SafetyOverride``, which is not a materialization concern). ``access_requirements`` may
    only ADD. ``availability_promise`` may only move LATER (§5.6) — see
    :func:`override_availability_promise` for why later is the safe direction and earlier is a caller
    error.

    Retention and cadence are deliberately NOT overridable. Ordering retention classes to decide
    which way is "stricter" would require the retention model §5.2 explicitly says not to invent, and
    a cadence override is a different declaration rather than a tightening of this one — which is
    also why an override carries no cadence of its own: it is read against the ONE clock the
    derivation was given, so a promise cannot be re-based without re-declaring the cadence.
    """

    sensitivity_class: str | None = None
    access_requirements: tuple[str, ...] = field(default_factory=tuple)
    availability_promise: AvailabilityPromiseV1 | None = None

    def __post_init__(self) -> None:
        if self.availability_promise is not None and not isinstance(
                self.availability_promise, AvailabilityPromiseV1):
            raise ValueError(
                f"override availability_promise must be an AvailabilityPromiseV1, got "
                f"{type(self.availability_promise).__name__} "
                f"({self.availability_promise!r}): §5.6 replaced the label with a canonical value "
                f"precisely so a promise is comparable, and a bare label cannot be ordered")
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
    availability_promise: AvailabilityPromiseV1
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
            "availability_promise": self.availability_promise.identity_payload(),
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
    availability_promise: AvailabilityPromiseV1,
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
        ValueError: ``overrides`` would LOOSEN the derived classification or move the availability
            promise EARLIER (§5.4 is monotonic in both), or the read set is empty / holds a ref the
            catalog cannot address. A declaration the caller has not yet made coherent is not a
            governed verdict, and §14 has no member for it.
    """
    # The promise override is settled BEFORE the catalog is read: it is a pure declaration check that
    # needs no governed fact, and a malformed declaration should not cost a classification query.
    # Both sides are read against the ONE cadence this derivation was given, so within a single
    # derivation the comparison basis matches by construction; `override_availability_promise` is
    # nonetheless the shared rule, because a later stage comparing a NEW declaration against an
    # already-persisted contract has two cadences and must get the incomparable case right.
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
        availability_promise=promise,
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
    availability_promise: AvailabilityPromiseV1,
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
                                  availability_promise=availability_promise, overrides=overrides)
        if isinstance(derived, MaterializationRefused):
            return derived
        contracts[ir.feature_name] = derived
    return group_by_contract(contracts)
