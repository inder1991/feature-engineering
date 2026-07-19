"""Phase 3C.2b-i-A · Task 2 — the multi-source typed contracts (spec §3/§4/§9).

The frozen/slotted dataclasses + lowercase-snake ``StrEnum``s every later task in A consumes, plus
the ONE constant ``PATH_AGG_TO_FUNCTION`` (spec §4) mapping A's per-path ``PathAggregation`` onto
the reused single-source ``AggregationFunction`` — or ``None`` for ``avg``/``stddev`` (no additive
or order-safe analog; validating them coarsely as SUM-sound would mislabel, so A fails them closed
as ``UNSUPPORTED_PATH_AGGREGATION``).

Pure data + one constant. NO behaviour, NO I/O, NO pydantic. The reuse model (spec §1) is enforced
by IMPORT, not redefinition: an operand's governed path is an ordinary single-source
``BindingPlanV1``; a plan's read inventory is a ``PhysicalReadSetV1``; the compile axis is the
existing ``ContractResolutionStatus``; additivity is the existing ``AdditivityClass``; per-path
declaration evidence is the existing ``HopAggregationV1``/``TemporalDeclarationV1``. A adds only the
multi-source carriers on top."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.planner.contracts import (
    ADDITIVITY_RULE_VERSION,
    AGGREGATION_RULE_VERSION,
    CONCEPT_REGISTRY_VERSION,
    MULTISOURCE_ASSEMBLY_VERSION,
    OPERATION_POLICY_VERSION,
    PLAN_CONTRACT_VERSION,
    PLANNER_BOUNDS_VERSION,
    RANKING_VERSION,
    SAFETY_EVALUATOR_VERSION,
    TEMPORAL_RULE_VERSION,
    AdditivityClass,
    AggregationFunction,
    BindingPlanV1,
    ContractResolutionStatus,
    DeclarationStatus,
    HopAggregationV1,
    PhysicalReadSetV1,
    TemporalDeclarationV1,
)

# ---------------------------------------------------------------------------
# StrEnums (spec §3/§4/§9) — lowercase snake_case values.
# ---------------------------------------------------------------------------


class SemanticRole(StrEnum):
    """The operation-typed role a slot plays in the final expression (spec §4 matrix). Validation is
    exact role->slot, never set membership."""
    measure = "measure"
    counted = "counted"
    time = "time"
    numerator = "numerator"
    denominator = "denominator"
    minuend = "minuend"
    subtrahend = "subtrahend"


class PathAggregation(StrEnum):
    """The per-operand roll-up A applies along its path to the common physical landing. Mapped onto
    the reused single-source ``AggregationFunction`` by ``PATH_AGG_TO_FUNCTION`` (spec §4)."""
    sum = "sum"
    min = "min"
    max = "max"
    take_latest = "take_latest"
    count = "count"     # type: ignore[assignment]  # deliberately shadows str.count on this StrEnum
    count_distinct = "count_distinct"
    avg = "avg"
    stddev = "stddev"


class FinalOperation(StrEnum):
    """The combination applied to the landed operands (spec §4 matrix)."""
    identity = "identity"
    count = "count"     # type: ignore[assignment]  # deliberately shadows str.count on this StrEnum
    count_distinct = "count_distinct"
    recency = "recency"
    trend = "trend"
    ratio = "ratio"
    difference = "difference"


class MultiSourceReason(StrEnum):
    """Every disposition in spec §9. `resolved` is the sole success; the rest partition into
    semantic (operand/path/landing/temporal governance), technical, and capture-incomplete."""
    resolved = "resolved"
    # semantic
    operand_shape_invalid = "operand_shape_invalid"
    unsupported_path_aggregation = "unsupported_path_aggregation"
    ordering_anchor_missing = "ordering_anchor_missing"
    no_governed_path = "no_governed_path"
    realization_endpoint_ungoverned = "realization_endpoint_ungoverned"
    no_common_physical_grain = "no_common_physical_grain"
    ambiguous_physical_grain = "ambiguous_physical_grain"
    aggregation_unsafe_on_path = "aggregation_unsafe_on_path"
    temporal_paths_incompatible = "temporal_paths_incompatible"
    source_binding_ungoverned = "source_binding_ungoverned"
    # technical
    operand_or_slot_not_preserved = "operand_or_slot_not_preserved"
    technical_failure = "technical_failure"
    # capture-incomplete
    budget_truncated = "budget_truncated"


# ---------------------------------------------------------------------------
# The one constant (spec §4). count_distinct -> order-safe count; avg/stddev -> None
# (not resolvable initially; fail-closed as UNSUPPORTED_PATH_AGGREGATION downstream).
# ---------------------------------------------------------------------------

PATH_AGG_TO_FUNCTION: dict[PathAggregation, AggregationFunction | None] = {
    PathAggregation.sum: AggregationFunction.sum,
    PathAggregation.min: AggregationFunction.min,
    PathAggregation.max: AggregationFunction.max,
    PathAggregation.take_latest: AggregationFunction.take_latest,
    PathAggregation.count: AggregationFunction.count,
    PathAggregation.count_distinct: AggregationFunction.count,
    PathAggregation.avg: None,
    PathAggregation.stddev: None,
}


# ---------------------------------------------------------------------------
# Input contracts (spec §3.2) — MultiSourcePlannerIntentV1 + its parts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathStrategyV1:
    """How ONE operand rolls up to the landing. ``ordering_anchor_concept`` is the *concept* of a
    temporal anchor (a bound need A injects as a second temporal need so the reused
    ``compile_temporal`` can validate ``take_latest``) — required iff aggregation == take_latest.
    ``external_type_required`` flags an operand whose output type must be externally validated."""
    aggregation: PathAggregation
    output_type: str
    output_additivity: AdditivityClass
    external_type_required: bool
    ordering_anchor_concept: str | None


@dataclass(frozen=True, slots=True)
class GovernedSourceBindingV1:
    """The operand's source-side authority: a governed grain entity + its composite (qualified) grain
    key columns + the deterministic grain ``fact_key`` proving them. There is NO key fact — source key
    columns come from the grain fact's columns (spec §2)."""
    source_grain_entity: str
    source_grain_key_refs: tuple[str, ...]
    grain_fact_key: str


@dataclass(frozen=True, slots=True)
class OperandSlotV1:
    slot_id: str
    semantic_role: SemanticRole
    catalog_source: str
    object_ref: str
    authoritative_concept: str
    path_strategy: PathStrategyV1
    source_binding: GovernedSourceBindingV1


@dataclass(frozen=True, slots=True)
class FinalExpressionV1:
    operation: FinalOperation
    ordered_slot_ids: tuple[str, ...]
    time_slot_id: str | None
    window: str | None
    output_additivity: AdditivityClass


@dataclass(frozen=True, slots=True)
class MultiSourcePlannerIntentV1:
    target_entity: str
    operands: tuple[OperandSlotV1, ...]
    final_expression: FinalExpressionV1
    operation_policy_version: str


# ---------------------------------------------------------------------------
# Governance + physical landing carriers (spec §3.1/§3.3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GovernedEndpointV1:
    """A path endpoint (source / intermediate / landing) revalidated against a VERIFIED grain fact.
    Keyed on the deterministic ``grain_fact_key`` (from ref+type), never a per-event id (finding #8).
    A missing/unverified grain fact => this is not a GovernedEndpointV1 (endpoint ungoverned)."""
    catalog: str
    table_ref: str
    grain_key_refs: tuple[str, ...]
    grain_fact_key: str


@dataclass(frozen=True, slots=True)
class PhysicalLandingV1:
    """The one physical grain every operand converges to; the final join is on EVERY key
    (``grain_key_refs`` is composite/multi-column)."""
    catalog: str
    table_ref: str
    grain_key_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OperandPathV1:
    """One operand's governed route to the landing. ``binding_plan`` is the frontier's own single-source
    ``BindingPlanV1`` — its ``path_segments`` ARE the governed crossings (realization authorities +
    VERIFIED bridge segments); no bespoke crossing carrier. ``governed_endpoints`` = source + each
    intermediate + landing, revalidated."""
    slot_id: str
    semantic_role: SemanticRole
    catalog_source: str
    object_ref: str
    binding_plan: BindingPlanV1
    governed_endpoints: tuple[GovernedEndpointV1, ...]
    path_strategy: PathStrategyV1
    pit_treatment: str


# ---------------------------------------------------------------------------
# Compile evidence (spec §3.3: "per-path HopAggregationV1/TemporalDeclarationV1 + final verdict").
# Reuses the single-source compiler's evidence types by import.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathDeclarationEvidenceV1:
    """One operand path's reused-compiler evidence: the fan-in ``HopAggregationV1``s and the
    ``TemporalDeclarationV1`` the per-path ``compile_aggregation``/``compile_temporal`` produced."""
    slot_id: str
    hop_aggregations: tuple[HopAggregationV1, ...]
    temporal_declaration: TemporalDeclarationV1 | None


@dataclass(frozen=True, slots=True)
class MultiSourceDeclarationEvidenceV1:
    """A plan's declaration evidence: per-path compiler evidence + the final-combination verdict."""
    per_path: tuple[PathDeclarationEvidenceV1, ...]
    final_verdict: DeclarationStatus
    final_reason_codes: tuple[MultiSourceReason, ...] = ()


# ---------------------------------------------------------------------------
# Output contracts (spec §3.3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MultiSourceBoundingMetricsV1:
    paths_per_operand_truncated: bool
    operand_combinations_truncated: bool
    states_truncated: bool
    landing_ambiguous: bool
    total_states_expanded: int


@dataclass(frozen=True, slots=True)
class MultiSourceReplayEnvelopeV1:
    """The input fingerprint material (findings #8/#11): all deterministic — target_entity + operand
    pins + source grain key refs + governed endpoint grain ``fact_key``s + bridge ``fact_key``s +
    versions. NO ``recipe_id``, NO per-event ids; double-run determinism keys off the stable
    ``fact_key``s. Version pins default; the computed ``input_hash`` is supplied by the fingerprinter."""
    target_entity: str
    operand_pins: tuple[str, ...]
    source_grain_key_refs: tuple[str, ...]
    governed_endpoint_fact_keys: tuple[str, ...]
    bridge_fact_keys: tuple[str, ...]
    input_hash: str
    multisource_assembly_version: str = MULTISOURCE_ASSEMBLY_VERSION
    operation_policy_version: str = OPERATION_POLICY_VERSION
    concept_registry_version: str = CONCEPT_REGISTRY_VERSION
    plan_contract_version: str = PLAN_CONTRACT_VERSION
    aggregation_rule_version: str = AGGREGATION_RULE_VERSION
    additivity_rule_version: str = ADDITIVITY_RULE_VERSION
    temporal_rule_version: str = TEMPORAL_RULE_VERSION
    safety_evaluator_version: str = SAFETY_EVALUATOR_VERSION
    planner_bounds_version: str = PLANNER_BOUNDS_VERSION
    ranking_version: str = RANKING_VERSION


@dataclass(frozen=True, slots=True)
class MultiSourceBindingPlanV1:
    """One candidate multi-source plan carrying ONLY its own compile result (contract axis + hashes +
    declaration evidence) — never a selected id (that lives on ``MultiSourcePlanningResultV1``)."""
    plan_id: str
    physical_landing: PhysicalLandingV1
    operand_paths: tuple[OperandPathV1, ...]
    final_expression: FinalExpressionV1
    physical_read_set: PhysicalReadSetV1
    resolution_status: MultiSourceReason
    reason_codes: tuple[MultiSourceReason, ...]
    contract_result_status: ContractResolutionStatus
    contract_id: str | None
    declaration_evidence: MultiSourceDeclarationEvidenceV1
    contract_input_hash: str
    contract_output_hash: str


@dataclass(frozen=True, slots=True)
class MultiSourcePlanningResultV1:
    """Mirrors ``BindingPlanningResultV1``: the run-level roll-up. Carries the selected ids (ingredient
    axis ``selected_plan_id``; contract axis ``selected_contract_plan_id``/``selected_contract_id``),
    the candidate plans, bounds, and the replay envelope."""
    run_id: str | None
    target_entity: str
    candidate_plans: tuple[MultiSourceBindingPlanV1, ...]
    selected_plan_id: str | None
    result_status: MultiSourceReason
    primary_reason_code: MultiSourceReason | None
    reason_codes: tuple[MultiSourceReason, ...]
    bounding: MultiSourceBoundingMetricsV1
    replay_envelope: MultiSourceReplayEnvelopeV1
    contract_result_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled
    selected_contract_plan_id: str | None = None
    selected_contract_id: str | None = None
