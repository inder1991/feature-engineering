"""Deterministic admission for a measured crosswalk (Release C Task 11, scope 3).

``bridge_admission``'s discipline, applied to three tables instead of two — and deliberately NOT a
second safety model. ``SafetyStatus``, ``ExecutionTier``, ``BridgeDisplayTier``,
``DirectionalCardinalityVerdictV1``, ``CardinalityBasis`` and the sandbox/production predicates are
the bridge family's, reused verbatim.

**No LLM, no review, no names.** Semantic recommendation may make a crosswalk worth measuring; only
governed metadata and a scope-matched measurement can establish execution safety. Human review is an
independent accountability axis and is never consulted here — the same rule the whole bridge family
keeps (`bridge_admission` module docstring, §5.8).

**THE TWO DIRECTIONS ARE ADMITTED INDEPENDENTLY.** They are SIDES of the canonically-ordered
definition, and a mapping table that is 1:1 forward and N:1 in reverse is ordinary. Admitting the
pair as one verdict would either refuse a usable direction or license an unusable one; both are
worse than two verdicts. ``forward`` and ``reverse`` therefore have separate safety statuses,
separate reason codes and separate sandbox/production answers.

**Sandbox vs production, exactly as the bridge family draws it.** Sandbox admits UNASSESSED evidence
— an unmeasured crosswalk is "discoverable, unmeasured", not a failure, and Task 12's generated
project carries an exact runtime gate that catches at execution what measurement has not yet
answered. Production requires CURRENT deterministic safety for BOTH legs AND the composed record:
two safe legs do not compose into a safe crosswalk, which is why the composed measurement is a
separate requirement rather than a formality.

**Evidence has to COVER the scope it is offered against.** ``bridge_admission`` has enforced both
halves of that since it shipped (``bridge_admission.py:509-517``) and both are carried here, over
three tables instead of two: partition-scoped evidence cannot validate an UNRESTRICTED applicability
scope, and a partition-RESTRICTED scope requires partition evidence. A crosswalk gives a subset three
places to enter — the mapping and either endpoint — and a subset anywhere scopes the composed
coverage, the composed fan-out and both uniqueness verdicts alike, which is why the question is asked
of :attr:`CrosswalkExecutionObservationV1.partition_scoped` (the content) rather than of one caveat.

**Nothing here executes.** The decision's output is a :class:`CrosswalkExecutionRevisionV1` — the
composition carrier Task 10 froze, which this module is the first producer of. There is still no
compiler and no executor; Task 12 owns those, and a pinned execution revision is what it will
compile FROM.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.relationship_observation import RelationshipMethod, RowCoverage
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.bridge_admission import BridgeDisplayTier
from featuregen.overlay.upload.bridge_assessment import EvidenceKind, EvidenceRefV1
from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    DirectionalCardinalityVerdictV1,
    RealizationApplicabilityScopeV1,
    SafetyStatus,
    eligible_for_production,
    eligible_for_sandbox,
)
from featuregen.overlay.upload.crosswalk import (
    CrosswalkDefinitionRevisionV1,
    CrosswalkExecutionRevisionV1,
    JoinLegKind,
    JoinLegPinV1,
)
from featuregen.overlay.upload.crosswalk_observation import (
    COMPOSED_DIRECTIONS,
    SOURCE_TO_TARGET,
    TARGET_TO_SOURCE,
    CrosswalkExecutionObservationV1,
    CrosswalkObservationCaveat,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality
from featuregen.overlay.upload.temporal_policy import DatasetRowSelectionV1

CROSSWALK_ADMISSION_POLICY_CONTRACT_VERSION = "1.0.0"


class CrosswalkAdmissionReason(StrEnum):
    """CLOSED vocabulary. A caller branches on these, never on message text."""

    # ── decision-level, hard ────────────────────────────────────────────────────────────────────
    MULTIPLE_ACTIVE_MAPPINGS = "multiple_active_mappings_for_endpoint_pair"
    DEFINITION_REVISION_NOT_CURRENT = "crosswalk_definition_revision_not_current"
    OBSERVATION_NOT_FOR_DEFINITION_REVISION = "observation_not_for_definition_revision"
    OBSERVATION_SCOPE_MISMATCH = "observation_scope_mismatch"
    LEG_NOT_CURRENTLY_EXECUTABLE = "leg_not_currently_executable"
    MAPPING_BINDING_MISMATCH = "observation_mapping_binding_mismatch"
    # ── decision-level, evidence quality ────────────────────────────────────────────────────────
    NOT_MEASURED = "crosswalk_not_measured"
    OBSERVATION_INCOMPLETE = "observation_incomplete"
    OBSERVATION_NOT_EXACT = "observation_not_exact"
    OBSERVATION_NOT_FULL_COVERAGE = "observation_not_full_coverage"
    OBSERVATION_TOO_OLD = "observation_too_old"
    BELOW_MINIMUM_ROWS = "observation_below_minimum_rows"
    NORMALIZATION_NOT_ADMITTED = "normalization_not_admitted"
    MAPPING_ROWS_NOT_TEMPORALLY_FILTERED = "mapping_rows_not_temporally_filtered"
    MAPPING_TEMPORAL_POLICY_UNRESOLVED = "mapping_temporal_policy_unresolved"
    MAPPING_NULL_ROWS = "mapping_null_rows"
    #: The two halves of ``bridge_admission``'s partition rule (``bridge_admission.py:509-517``),
    #: carried across to three tables. Named for the crosswalk rather than the realization because
    #: that is what the reader is looking at; the RULE is the bridge family's, unchanged.
    PARTITION_SCOPED_EVIDENCE_CANNOT_VALIDATE_UNRESTRICTED = (
        "partition_scoped_evidence_cannot_validate_unrestricted_crosswalk")
    SCOPE_REQUIRES_PARTITION_EVIDENCE = "crosswalk_scope_requires_partition_evidence"
    # ── per direction ───────────────────────────────────────────────────────────────────────────
    DIRECTIONAL_FANOUT = "directional_crosswalk_fanout"
    DIRECTIONAL_UNIQUENESS_NOT_PROVED = "directional_uniqueness_not_proved"
    LOW_SOURCE_CONTAINMENT = "source_containment_below_policy"
    LOW_TARGET_CONTAINMENT = "target_containment_below_policy"
    # ── the one positive ────────────────────────────────────────────────────────────────────────
    POLICY_SATISFIED = "deterministic_crosswalk_policy_satisfied"


@dataclass(frozen=True, slots=True)
class CrosswalkAdmissionPolicyV1:
    """Versioned automatic-safety policy for a measured crosswalk.

    A full exact composed measurement is not a statistical sample, so the default floor is one
    non-empty composed row. Deployments may raise the floor without changing the contract.
    """

    minimum_composed_rows: int = 1
    minimum_mapping_rows: int = 1
    maximum_evidence_age: timedelta = timedelta(days=30)
    #: A mapping row with a NULL on either side connects nothing and is measured as such; a policy
    #: that tolerated them would admit a crosswalk whose coverage it cannot describe.
    maximum_mapping_null_ratio: float = 0.0
    minimum_source_distinct_containment: float = 0.0
    minimum_target_distinct_containment: float = 0.0
    allowed_normalization_ids: tuple[str, ...] = ("identity_v1",)
    #: When true, an unfiltered mapping measurement can never reach production. Default TRUE: a
    #: mapping table nobody has declared a row policy for may be full SCD history, and uniqueness
    #: over history is a different claim from uniqueness now.
    require_governed_mapping_rows_for_production: bool = True

    def __post_init__(self) -> None:
        if self.minimum_composed_rows < 1 or self.minimum_mapping_rows < 1:
            raise ValueError("crosswalk admission minimums must be positive")
        if self.maximum_evidence_age <= timedelta(0):
            raise ValueError("maximum evidence age must be positive")
        for value in (self.maximum_mapping_null_ratio,
                      self.minimum_source_distinct_containment,
                      self.minimum_target_distinct_containment):
            if not 0.0 <= value <= 1.0:
                raise ValueError("crosswalk admission ratios must be in [0,1]")
        if len(set(self.allowed_normalization_ids)) != len(self.allowed_normalization_ids):
            raise ValueError("allowed normalization ids must not contain duplicates")

    @property
    def policy_version(self) -> str:
        return "crosswalk-admission-v1:" + canonical_hash(self.identity_payload())[:16]

    def identity_payload(self) -> dict[str, object]:
        return {
            "contract_version": CROSSWALK_ADMISSION_POLICY_CONTRACT_VERSION,
            "minimum_composed_rows": self.minimum_composed_rows,
            "minimum_mapping_rows": self.minimum_mapping_rows,
            "maximum_evidence_age_seconds": int(self.maximum_evidence_age.total_seconds()),
            "maximum_mapping_null_ratio": self.maximum_mapping_null_ratio,
            "minimum_source_distinct_containment": self.minimum_source_distinct_containment,
            "minimum_target_distinct_containment": self.minimum_target_distinct_containment,
            "allowed_normalization_ids": list(self.allowed_normalization_ids),
            "require_governed_mapping_rows_for_production":
                self.require_governed_mapping_rows_for_production,
        }


@dataclass(frozen=True, slots=True)
class CrosswalkDirectionVerdictV1:
    """One NAMED direction's verdict. The two are independent by design."""

    direction: str
    cardinality: DirectionalCardinalityVerdictV1
    cardinality_basis: CardinalityBasis
    safety_status: SafetyStatus
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.direction not in COMPOSED_DIRECTIONS:
            raise ValueError(f"direction must be one of {COMPOSED_DIRECTIONS}")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    @property
    def sandbox_admissible(self) -> bool:
        """UNASSESSED evidence admits sandbox — as DATA for Task 12's exact runtime gate, never as
        a claim that the direction is safe. UNSAFE does not: a measured fan-out is an answer."""
        return self.safety_status in {
            SafetyStatus.UNASSESSED, SafetyStatus.DETERMINISTICALLY_VALIDATED}

    @property
    def production_admissible(self) -> bool:
        return self.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED


@dataclass(frozen=True, slots=True)
class CrosswalkAdmissionDecisionV1:
    """What one measured crosswalk may be used for, per direction, and why."""

    crosswalk_definition_revision_id: str
    scope_id: str
    observation_revision_id: str | None
    forward: CrosswalkDirectionVerdictV1
    reverse: CrosswalkDirectionVerdictV1
    display_tier: BridgeDisplayTier
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[EvidenceRefV1, ...]
    policy_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))

    def verdict(self, direction: str) -> CrosswalkDirectionVerdictV1:
        if direction == SOURCE_TO_TARGET:
            return self.forward
        if direction == TARGET_TO_SOURCE:
            return self.reverse
        raise ValueError(f"direction must be one of {COMPOSED_DIRECTIONS}")

    @property
    def measured(self) -> bool:
        return self.observation_revision_id is not None

    @property
    def any_production_admissible(self) -> bool:
        return self.forward.production_admissible or self.reverse.production_admissible

    @property
    def combined_safety_status(self) -> SafetyStatus:
        """The WEAKEST of the two directions — what the crosswatch as a whole may claim.

        Deliberately the weakest and not the strongest: an execution revision carries one status,
        and a 1:1-forward/N:1-reverse crosswalk that recorded DETERMINISTICALLY_VALIDATED there
        would license the reverse traversal too. The per-direction verdicts are where the usable
        direction survives."""
        statuses = {self.forward.safety_status, self.reverse.safety_status}
        if SafetyStatus.UNSAFE in statuses:
            return SafetyStatus.UNSAFE
        if SafetyStatus.UNASSESSED in statuses:
            return SafetyStatus.UNASSESSED
        return SafetyStatus.DETERMINISTICALLY_VALIDATED


def _observation_evidence(
    observation: CrosswalkExecutionObservationV1,
) -> EvidenceRefV1:
    kind = (EvidenceKind.EXACT_PROFILE if observation.method == RelationshipMethod.EXACT.value
            else EvidenceKind.APPROXIMATE_PROFILE)
    return EvidenceRefV1(
        evidence_id=observation.observation_revision_id, kind=kind,
        producer=observation.producer,
        content_hash=materialize_hash(observation.content_payload()),
        observed_at=observation.observed_at)


def _unknown_verdict(direction: str, reasons: list[str],
                     safety: SafetyStatus) -> CrosswalkDirectionVerdictV1:
    return CrosswalkDirectionVerdictV1(
        direction=direction, cardinality=DirectionalCardinalityVerdictV1.unknown(),
        cardinality_basis=CardinalityBasis.NONE, safety_status=safety,
        reason_codes=tuple(reasons))


def evaluate_crosswalk_admission(
    *,
    crosswalk_definition_revision_id: str,
    scope: RealizationApplicabilityScopeV1,
    policy: CrosswalkAdmissionPolicyV1,
    now: datetime,
    observation: CrosswalkExecutionObservationV1 | None = None,
    active_mapping_count: int = 1,
    definition_revision_current: bool = True,
    legs_currently_executable: bool = True,
    mapping_binding_revision_id: str | None = None,
) -> CrosswalkAdmissionDecisionV1:
    """Fuse the composed measurement with the current state of what it stands on.

    ``active_mapping_count`` is how many ACTIVE mapping definitions relate this endpoint pair. More
    than one is refused outright (§6.6 "V1 conflict behaviour is only ``refuse_on_multiple``"): with
    two active mappings the answer depends on which one a compiler happened to read, and no
    deduplication, first-row pick or tie-break is permitted to make that go away.
    """
    evidence: tuple[EvidenceRefV1, ...] = ()
    if observation is not None:
        evidence = (_observation_evidence(observation),)

    hard: list[str] = []
    if active_mapping_count > 1:
        hard.append(CrosswalkAdmissionReason.MULTIPLE_ACTIVE_MAPPINGS.value)
    if not definition_revision_current:
        hard.append(CrosswalkAdmissionReason.DEFINITION_REVISION_NOT_CURRENT.value)
    if not legs_currently_executable:
        hard.append(CrosswalkAdmissionReason.LEG_NOT_CURRENTLY_EXECUTABLE.value)
    if observation is not None:
        if observation.crosswalk_definition_revision_id != crosswalk_definition_revision_id:
            hard.append(
                CrosswalkAdmissionReason.OBSERVATION_NOT_FOR_DEFINITION_REVISION.value)
        if observation.scope_id != scope.scope_id:
            hard.append(CrosswalkAdmissionReason.OBSERVATION_SCOPE_MISMATCH.value)
        if (mapping_binding_revision_id is not None
                and observation.mapping_binding_revision_id != mapping_binding_revision_id):
            hard.append(CrosswalkAdmissionReason.MAPPING_BINDING_MISMATCH.value)
    if hard:
        return CrosswalkAdmissionDecisionV1(
            crosswalk_definition_revision_id=crosswalk_definition_revision_id,
            scope_id=scope.scope_id,
            observation_revision_id=(
                None if observation is None else observation.observation_revision_id),
            forward=_unknown_verdict(SOURCE_TO_TARGET, hard, SafetyStatus.UNSAFE),
            reverse=_unknown_verdict(TARGET_TO_SOURCE, hard, SafetyStatus.UNSAFE),
            display_tier=BridgeDisplayTier.REJECTED, reason_codes=tuple(hard),
            evidence_refs=evidence, policy_version=policy.policy_version)

    if observation is None:
        # DISCOVERABLE, UNMEASURED. Not a failure and not a refusal — the no-blocked rule. Sandbox
        # may still use it as data for Task 12's exact runtime gate; production may not.
        reasons = [CrosswalkAdmissionReason.NOT_MEASURED.value]
        return CrosswalkAdmissionDecisionV1(
            crosswalk_definition_revision_id=crosswalk_definition_revision_id,
            scope_id=scope.scope_id, observation_revision_id=None,
            forward=_unknown_verdict(SOURCE_TO_TARGET, reasons, SafetyStatus.UNASSESSED),
            reverse=_unknown_verdict(TARGET_TO_SOURCE, reasons, SafetyStatus.UNASSESSED),
            display_tier=BridgeDisplayTier.DISCOVERED, reason_codes=tuple(reasons),
            evidence_refs=evidence, policy_version=policy.policy_version)

    shared = _shared_reasons(observation, policy=policy, now=now, scope=scope)
    forward = _direction_verdict(
        observation, SOURCE_TO_TARGET, policy=policy, shared=shared)
    reverse = _direction_verdict(
        observation, TARGET_TO_SOURCE, policy=policy, shared=shared)
    tier = (BridgeDisplayTier.STRONG_PROPOSED
            if forward.production_admissible or reverse.production_admissible
            else BridgeDisplayTier.DISCOVERED)
    return CrosswalkAdmissionDecisionV1(
        crosswalk_definition_revision_id=crosswalk_definition_revision_id,
        scope_id=scope.scope_id,
        observation_revision_id=observation.observation_revision_id,
        forward=forward, reverse=reverse, display_tier=tier,
        reason_codes=tuple(shared), evidence_refs=evidence,
        policy_version=policy.policy_version)


def _shared_reasons(
    observation: CrosswalkExecutionObservationV1, *,
    policy: CrosswalkAdmissionPolicyV1, now: datetime,
    scope: RealizationApplicabilityScopeV1,
) -> list[str]:
    """Everything that disqualifies BOTH directions — evidence quality, not shape.

    ``scope`` is here for the partition rule and only for it: the question "does this evidence
    cover what this scope claims?" cannot be answered from the measurement alone, and the two are
    fused nowhere else.
    """
    reasons: list[str] = []
    if not observation.complete:
        reasons.append(CrosswalkAdmissionReason.OBSERVATION_INCOMPLETE.value)
    if observation.method != RelationshipMethod.EXACT.value:
        reasons.append(CrosswalkAdmissionReason.OBSERVATION_NOT_EXACT.value)
    if observation.row_coverage is not RowCoverage.FULL:
        reasons.append(CrosswalkAdmissionReason.OBSERVATION_NOT_FULL_COVERAGE.value)
    if now - observation.observed_at > policy.maximum_evidence_age:
        reasons.append(CrosswalkAdmissionReason.OBSERVATION_TOO_OLD.value)
    if (observation.composed_row_count < policy.minimum_composed_rows
            or observation.mapping.row_count < policy.minimum_mapping_rows):
        reasons.append(CrosswalkAdmissionReason.BELOW_MINIMUM_ROWS.value)
    if observation.mapping.row_count:
        null_ratio = observation.mapping.null_row_count / observation.mapping.row_count
        if null_ratio > policy.maximum_mapping_null_ratio:
            reasons.append(CrosswalkAdmissionReason.MAPPING_NULL_ROWS.value)
    for leg in (observation.source_leg, observation.target_leg):
        if any(item not in policy.allowed_normalization_ids for item in leg.normalization_ids):
            # No model-proposed free-form transform can pass this closed vocabulary.
            reasons.append(CrosswalkAdmissionReason.NORMALIZATION_NOT_ADMITTED.value)
    caveats = set(observation.caveats)
    if policy.require_governed_mapping_rows_for_production:
        if CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_ABSENT.value in caveats:
            reasons.append(
                CrosswalkAdmissionReason.MAPPING_ROWS_NOT_TEMPORALLY_FILTERED.value)
        if CrosswalkObservationCaveat.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value in caveats:
            reasons.append(CrosswalkAdmissionReason.MAPPING_TEMPORAL_POLICY_UNRESOLVED.value)
    # ── the partition rule, BOTH halves (bridge_admission.py:509-517) ───────────────────────────
    #
    # This is not a policy knob and has no opt-out, exactly as in the bridge family. Uniqueness
    # proved over one partition of one of three tables is a statement about that partition; a scope
    # that restricts nothing is a claim about all of them, and the gap between the two is where a
    # crosswalk silently multiplies a population. The mirror error is as real: a scope that DOES
    # restrict to partitions, validated by a measurement that read everything, was proved on rows
    # the scope does not cover.
    #
    # V1 compares PRESENCE and not values, deliberately and identically to the bridge rule:
    # `partition_scope_ref` is an opaque governed reference with no resolver in this repo, so a
    # value-level match would have to invent one — and an invented match that returned True would
    # be worse than no check at all. Sandbox stays admissible throughout (UNASSESSED, via the
    # shared-reason path): the evidence is real, it is DATA for Task 12's exact runtime gate, and
    # calling it a failure is the thing the no-blocked rule forbids.
    partition_scoped = observation.partition_scoped
    if scope.partition_scope_ref is None and partition_scoped:
        reasons.append(
            CrosswalkAdmissionReason.PARTITION_SCOPED_EVIDENCE_CANNOT_VALIDATE_UNRESTRICTED.value)
    if scope.partition_scope_ref is not None and not partition_scoped:
        reasons.append(CrosswalkAdmissionReason.SCOPE_REQUIRES_PARTITION_EVIDENCE.value)
    if observation.source_distinct_containment_ratio < (
            policy.minimum_source_distinct_containment):
        reasons.append(CrosswalkAdmissionReason.LOW_SOURCE_CONTAINMENT.value)
    if observation.target_distinct_containment_ratio < (
            policy.minimum_target_distinct_containment):
        reasons.append(CrosswalkAdmissionReason.LOW_TARGET_CONTAINMENT.value)
    return reasons


def _direction_verdict(
    observation: CrosswalkExecutionObservationV1, direction: str, *,
    policy: CrosswalkAdmissionPolicyV1, shared: list[str],
) -> CrosswalkDirectionVerdictV1:
    """One direction, assessed on its OWN measured fan-out."""
    forward = direction == SOURCE_TO_TARGET
    observed_max = (observation.source_to_target_max_matches if forward
                    else observation.target_to_source_max_matches)
    reverse_max = (observation.target_to_source_max_matches if forward
                   else observation.source_to_target_max_matches)
    uniqueness = observation.uniqueness_verdict(direction)

    if uniqueness == "not_unique":
        # MEASURED fan-out. An answer, not an absence — and a hard one: traversing this direction
        # multiplies the population, which is the defect the whole release exists to prevent.
        cardinality = DirectionalCardinalityVerdictV1(
            Cardinality.ONE_TO_MANY if reverse_max <= 1 else Cardinality.MANY_TO_MANY)
        return CrosswalkDirectionVerdictV1(
            direction=direction, cardinality=cardinality,
            cardinality_basis=CardinalityBasis.EXACT_PROFILE,
            safety_status=SafetyStatus.UNSAFE,
            reason_codes=(*shared, CrosswalkAdmissionReason.DIRECTIONAL_FANOUT.value))

    if uniqueness == "unknown" or shared:
        return CrosswalkDirectionVerdictV1(
            direction=direction, cardinality=DirectionalCardinalityVerdictV1.unknown(),
            cardinality_basis=(CardinalityBasis.EXACT_PROFILE if observation.exact_and_complete
                               else CardinalityBasis.NONE),
            safety_status=SafetyStatus.UNASSESSED,
            reason_codes=(*shared,
                          CrosswalkAdmissionReason.DIRECTIONAL_UNIQUENESS_NOT_PROVED.value))

    cardinality = DirectionalCardinalityVerdictV1(
        Cardinality.ONE_TO_ONE if reverse_max <= 1 else Cardinality.MANY_TO_ONE)
    assert observed_max <= 1                      # `unique` is exactly this, read from the record
    return CrosswalkDirectionVerdictV1(
        direction=direction, cardinality=cardinality,
        cardinality_basis=CardinalityBasis.EXACT_PROFILE,
        safety_status=SafetyStatus.DETERMINISTICALLY_VALIDATED,
        reason_codes=(CrosswalkAdmissionReason.POLICY_SATISFIED.value,))


# ── leg pins, resolved through their REAL owners ────────────────────────────────────────────────

def leg_pin_from_realization(
    revision: BridgeJoinRealizationRevisionV1,
    current: BridgeRealizationCurrentV1,
    *,
    require_production: bool = False,
) -> JoinLegPinV1:
    """A CROSS-catalog leg, pinned from ONE current governed bridge realization — its real owner.

    Refuses a realization that is not currently eligible: a pin is a claim about what a compilation
    may read, and pinning a demoted or retired realization would carry that claim past the
    revalidation that withdrew it.
    """
    eligible = (eligible_for_production(revision, current) if require_production
                else eligible_for_sandbox(revision, current))
    if not eligible:
        raise ValueError(
            f"bridge realization {revision.realization_revision_id} is not currently "
            f"{'production' if require_production else 'sandbox'}-eligible, so a crosswalk leg "
            "cannot pin it")
    return JoinLegPinV1(
        kind=JoinLegKind.CROSS_CATALOG,
        plan_hash=revision.realization_revision_id,
        from_dataset_ref=revision.from_endpoint.logical_table_ref,
        to_dataset_ref=revision.to_endpoint.logical_table_ref,
        from_binding_revision_id=str(revision.from_endpoint.binding_revision_id),
        to_binding_revision_id=str(revision.to_endpoint.binding_revision_id),
        read_set_hash=materialize_hash({
            "column_pairs": [pair.identity_payload() for pair in revision.column_pairs],
            "predicates": [p.identity_payload() for p in revision.predicates],
        }),
        fact_keys=(revision.bridge_fact_key,),
        realization_revision_ids=(revision.realization_revision_id,),
        dependency_snapshot_ids=(revision.dependency_snapshot_id,),
        predicate_content_hashes=tuple(
            materialize_hash(p.identity_payload()) for p in revision.predicates),
    )


def leg_pin_from_join_plan(
    plan,
    *,
    from_dataset_ref: str,
    to_dataset_ref: str,
    from_binding_revision_id: str,
    to_binding_revision_id: str,
    predicate_content_hashes: tuple[str, ...] = (),
) -> JoinLegPinV1:
    """A SAME-catalog leg, pinned from the ``materialize.joins.JoinPlan`` its real owner produced.

    The plan is passed IN rather than planned here, and that is the seam, not a shortcut: admission
    is a governance module and importing the planner would put the execution path Task 12 owns
    inside it. What this function guarantees is that a same-catalog leg is pinned from a real plan
    — the steps it actually resolved, hashed — and never from a bridge realization, which is the
    "pretend every leg is an entity bridge" defect ``JoinLegPinV1`` refuses structurally.
    """
    steps = getattr(plan, "steps", None)
    if steps is None:
        raise ValueError(
            "a same-catalog crosswalk leg is pinned from a governed JoinPlan; a bare hash cannot "
            "be replayed or explained")
    step_payload = [_step_payload(step) for step in steps]
    return JoinLegPinV1(
        kind=JoinLegKind.SAME_CATALOG,
        plan_hash=materialize_hash({
            "steps": step_payload,
            "outcome_kind": getattr(plan, "outcome_kind", ""),
            "roles_used": sorted(getattr(plan, "roles_used", ()) or ()),
        }),
        from_dataset_ref=from_dataset_ref,
        to_dataset_ref=to_dataset_ref,
        from_binding_revision_id=from_binding_revision_id,
        to_binding_revision_id=to_binding_revision_id,
        read_set_hash=materialize_hash({"steps": step_payload}),
        predicate_content_hashes=predicate_content_hashes,
    )


def _step_payload(step) -> dict[str, object]:
    """A step's identity, read reflectively so a planner field addition cannot silently drop out of
    the hash without this raising instead."""
    payload = {}
    for name in ("from_table", "to_table", "from_column", "to_column", "cardinality",
                 "fact_key", "approved_join_fact_key", "realization_revision_id"):
        value = getattr(step, name, None)
        if value is not None:
            payload[name] = str(value)
    if not payload:
        raise ValueError(f"join step {type(step).__name__} exposes no identity fields to pin")
    return payload


# ── the first producer of the Task-10 execution shape ───────────────────────────────────────────

def admitted_crosswalk_execution(
    decision: CrosswalkAdmissionDecisionV1,
    *,
    source_leg: JoinLegPinV1,
    target_leg: JoinLegPinV1,
    mapping_binding_revision_id: str,
    scope: RealizationApplicabilityScopeV1,
    observation: CrosswalkExecutionObservationV1 | None,
    mapping_temporal_policy_revision_id: str | None = None,
) -> CrosswalkExecutionRevisionV1:
    """Create the immutable composition carrier one admission decision selected.

    THE FIRST PRODUCER of :class:`CrosswalkExecutionRevisionV1`, which Task 10 froze as a shape with
    no producer. It is still not executable: nothing compiles or runs one, and Task 12 owns both.

    The combined safety status is the WEAKEST direction's, and the contract's own rule then applies
    — deterministic validation is impossible without a measurement per leg AND one composed
    observation after temporal filtering.

    BOTH evidence fields are carried, each naming what actually exists: ``leg_measurement_ids`` are
    the two ``clo_`` content-addressed leg measurements (always present, they live inside the
    composed observation), and ``leg_observation_revision_ids`` are the ``rob_`` rows in the shipped
    two-endpoint store — present for exactly the legs that have one, which for the ordinary
    one-same-catalog/one-cross-catalog shape is one leg. Putting the measurement hashes under the
    row-id name, as the first draft did, made the contract's gate satisfiable with values no store
    holds.
    """
    if decision.observation_revision_id is not None and (
            observation is None
            or observation.observation_revision_id != decision.observation_revision_id):
        raise ValueError(
            "the execution revision must pin the SAME composed observation the decision was taken "
            "on; a revision naming another measurement records a verdict nobody reached")
    measurement_ids = () if observation is None else observation.leg_measurement_ids
    persisted_ids = () if observation is None else observation.leg_observation_revision_ids
    return CrosswalkExecutionRevisionV1(
        crosswalk_definition_revision_id=decision.crosswalk_definition_revision_id,
        mapping_binding_revision_id=mapping_binding_revision_id,
        source_leg=source_leg,
        target_leg=target_leg,
        applicability_scope=scope,
        combined_cardinality=decision.forward.cardinality,
        safety_status=decision.combined_safety_status,
        mapping_temporal_policy_revision_id=mapping_temporal_policy_revision_id,
        leg_measurement_ids=measurement_ids,
        leg_observation_revision_ids=persisted_ids,
        composition_observation_revision_id=(
            None if observation is None else observation.observation_revision_id),
    )


# ── what a compiler compiles FROM (Release C Task 12) ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AdmittedCrosswalkV1:
    """One admitted crosswalk, assembled into the ONE object a compilation may plan a traversal on.

    Task 11 produces four separate governed artifacts — a definition revision, a composed
    observation, an admission decision and the execution revision the decision selected — and a
    compiler that took them as four parameters could be handed four that describe different things.
    So they are fused here, ONCE, with every cross-reference checked at construction; Task 12's
    planner then reads a bundle it cannot have assembled inconsistently.

    **Why the DECISION travels with the execution revision.** The revision carries one
    ``combined_cardinality``, and ``admitted_crosswalk_execution`` fills it from
    ``decision.forward.cardinality`` — so the revision alone can answer the FORWARD direction and
    nothing else. The reverse direction has its own verdict, its own reason codes and its own
    sandbox/production answers (that independence is the whole design of this module), and a
    traversal planned in reverse must be gated on THAT verdict, not on the forward one wearing a
    "combined" name. :meth:`verdict_for` is the one accessor.

    ``mapping_row_selection`` is Release B's resolved answer, passed IN. This module does not
    resolve it and neither does the compiler: the temporal resolver owns which rows of a dataset
    answer a question, and a second resolution here would be a second opinion about the row rule the
    uniqueness was measured under. What IS checked here is that the selection is the one the pins
    name — same dataset, same policy revision, and the same content hash the composed measurement
    recorded.

    **THE THREE PHYSICAL BINDINGS ARE PART OF THE BUNDLE, and they are not optional.** A crosswalk's
    pins are revision IDS — ``mapping_binding_revision_id`` on the execution, two per leg on the
    pins — and an id cannot be compared with a table a compilation RESOLVED onto the cluster. So the
    bindings those ids name travel here too, and the compiler compares each resolved
    :class:`~featuregen.materialize.joins.PhysicalIdentity` against the one that was measured
    (``joins.plan_crosswalk_join``). Without them, re-pointing an environment's schema map moved all
    three tables of a measured crosswalk and the traversal planned cleanly against tables nobody
    measured — the exact hazard ``joins._refuse_unpinned_mapping`` claims to close and closes only
    for the mapping dataset's LOGICAL name. They are required rather than defaulted for the reason
    every other field here is checked: an optional safety input is one a caller can omit, and the
    omission looks like the safe case.
    """

    definition: CrosswalkDefinitionRevisionV1
    execution: CrosswalkExecutionRevisionV1
    decision: CrosswalkAdmissionDecisionV1
    #: The three tables as the measurement pinned them, by the DEFINITION's canonical sides. Which
    #: of ``source``/``target`` a traversal starts from is the compiler's question (it depends on
    #: the requested direction), never this bundle's.
    source_binding: PhysicalDatasetBindingV1
    target_binding: PhysicalDatasetBindingV1
    mapping_binding: PhysicalDatasetBindingV1
    mapping_row_selection: DatasetRowSelectionV1 | None = None
    observation: CrosswalkExecutionObservationV1 | None = None

    def __post_init__(self) -> None:
        revision_id = self.definition.revision_id
        if self.execution.crosswalk_definition_revision_id != revision_id:
            raise ValueError(
                "the execution revision pins a different crosswalk definition revision than the "
                f"one supplied ({self.execution.crosswalk_definition_revision_id} != "
                f"{revision_id}): a plan compiled from this pair would join the columns of one "
                "definition under the safety verdict of another")
        if self.decision.crosswalk_definition_revision_id != revision_id:
            raise ValueError(
                "the admission decision was taken on a different crosswalk definition revision "
                f"({self.decision.crosswalk_definition_revision_id} != {revision_id})")
        if self.decision.observation_revision_id != (
                self.execution.composition_observation_revision_id):
            raise ValueError(
                "the decision and the execution revision name different composed observations; a "
                "verdict read off one measurement and pinned against another is a verdict nobody "
                "reached")
        if self.observation is not None and (
                self.observation.observation_revision_id
                != self.execution.composition_observation_revision_id):
            raise ValueError(
                "the supplied composed observation is not the one the execution revision pins")
        selection = self.mapping_row_selection
        if selection is not None:
            if selection.dataset_logical_ref != self.definition.mapping_dataset_ref:
                raise ValueError(
                    f"the mapping row selection is for {selection.dataset_logical_ref!r} and the "
                    f"crosswalk's mapping dataset is {self.definition.mapping_dataset_ref!r}")
            pinned_policy = self.execution.mapping_temporal_policy_revision_id
            if (pinned_policy is not None
                    and selection.temporal_policy_revision_id != pinned_policy):
                raise ValueError(
                    "the mapping row selection resolved under temporal policy "
                    f"{selection.temporal_policy_revision_id} and the execution revision pins "
                    f"{pinned_policy}: the current policy pointer moved after the crosswalk was "
                    "measured, so the rows this traversal would read are not the rows its "
                    "uniqueness was proved over")
            measured = (None if self.observation is None
                        else self.observation.mapping_row_selection_hash)
            if measured is not None and selection.content_hash != measured:
                raise ValueError(
                    "the mapping row selection's content hash differs from the one the composed "
                    f"measurement recorded ({selection.content_hash} != {measured})")
        self._check_bindings()

    def _check_bindings(self) -> None:
        """Each supplied binding must BE the revision the execution pinned, for the RIGHT table.

        Two questions, and the first does not answer the second. ``binding_revision_id`` is a
        content hash over the binding, so an equal id proves the object is the pinned revision —
        but not that the pinned revision belongs in the slot it was handed to. The leg pins name
        their datasets, so the second question has a governed answer already recorded and this
        checks against it rather than inventing one.

        The mapping binding is checked THREE ways because three pins name it and a crosswalk whose
        two legs travelled through different mapping tables is not a crosswalk.
        """
        source_leg, target_leg = self.execution.source_leg, self.execution.target_leg
        for binding, pinned_id, dataset_ref, side in (
                (self.source_binding, source_leg.from_binding_revision_id,
                 source_leg.from_dataset_ref, "source"),
                (self.target_binding, target_leg.to_binding_revision_id,
                 target_leg.to_dataset_ref, "target"),
                (self.mapping_binding, self.execution.mapping_binding_revision_id,
                 self.definition.mapping_dataset_ref, "mapping"),
                (self.mapping_binding, source_leg.to_binding_revision_id,
                 source_leg.to_dataset_ref, "mapping (as the source leg's destination)"),
                (self.mapping_binding, target_leg.from_binding_revision_id,
                 target_leg.from_dataset_ref, "mapping (as the target leg's origin)")):
            if binding.binding_revision_id != pinned_id:
                raise ValueError(
                    f"the supplied {side} binding is revision {binding.binding_revision_id} and "
                    f"the execution pins {pinned_id}: a traversal planned against a binding the "
                    "measurement did not pin would read a table whose uniqueness nobody measured")
            catalog, remainder = dataset_ref.split("::", 1)
            expected = (catalog.strip().lower(), remainder.split(".")[-1].strip().lower())
            actual = (binding.identity.catalog_source.strip().lower(),
                      binding.identity.table.strip().lower())
            if actual != expected:
                raise ValueError(
                    f"the {side} binding addresses {actual[0]}::{actual[1]} and the pin names "
                    f"{dataset_ref!r}: the right revision id in the wrong slot would compare a "
                    "resolved table against another table's pinned address")

    def verdict_for(self, direction: str) -> CrosswalkDirectionVerdictV1:
        """The verdict for ONE requested direction — never ``execution.combined_cardinality``."""
        return self.decision.verdict(direction)

    def endpoint_refs(self) -> tuple[str, str]:
        """``(source side, target side)`` LOGICAL TABLE refs, in the definition's canonical order."""
        return (self.definition.source_endpoint.logical_table_ref,
                self.definition.target_endpoint.logical_table_ref)
