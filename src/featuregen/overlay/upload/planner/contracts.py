"""Phase-3B.3a — cross-catalog binding planner contracts + reason-code/status vocabulary.

The BACKBONE reused by 3B.3b/c/3B.4/3C. Supersedes the parent 3B design's CrossCatalog* names.
Frozen dataclasses; lowercase snake_case StrEnum values. No behaviour — pure contracts + constants,
plus the two pure identity authorities: `make_binding_plan` (3B.3b — mints physical_plan_id under the
frozen PHYSICAL_PLAN_VERSION) and `make_contract_id` (3B.3c — the freshness-free declaration identity)."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

PLANNER_VERSION = "3b3a.1.0.0"
# The BindingPlanV1 SCHEMA version (contract shape). 3B.3c split it out of the physical-id
# material: it may bump freely as fields are added without moving any physical_plan_id.
PLAN_CONTRACT_VERSION = "3b3c.1.0.0"
# FROZEN (F1): the version hashed into every physical_plan_id. It pins the physical-path
# derivation, NOT the dataclass shape — it must never track PLAN_CONTRACT_VERSION bumps, or every
# stored physical id would silently move. Bump ONLY on a change to the physical-id material itself.
PHYSICAL_PLAN_VERSION = "3b3b.1.0.0"
REASON_CODE_REGISTRY_VERSION = "1.2.0"
# 3B.3c contract-compiler rule versions — each hashed into contract_id (via make_contract_id) or
# pinned in the replay envelope, so a rule change can never masquerade as the same contract:
AGGREGATION_RULE_VERSION = "1.0.0"
ADDITIVITY_RULE_VERSION = "1.0.0"
TEMPORAL_RULE_VERSION = "1.0.0"
SAFETY_EVALUATOR_VERSION = "1.0.0"
DRIFT_FRESHNESS_SLA_VERSION = "1.0.0"
PLANNER_BOUNDS_VERSION = "1.0.0"
RANKING_VERSION = "1.0.0"
# Version pins for inputs that have no formal version source yet (wired to real policy versions in 3C):
READ_SCOPE_POLICY_VERSION = "1.0.0"
ROLE_RESOLUTION_VERSION = "unknown"
RECIPE_REGISTRY_VERSION = "1.0.0"
APPLICABILITY_MAPPING_VERSION = "1.0.0"
CONCEPT_REGISTRY_VERSION = "concepts@1"

# Bounds (conservative; tunable). Truncation is always recorded, never a pretend-complete result.
MAX_CANDIDATE_COLUMNS_PER_NEED_PER_CATALOG = 8
MAX_PARTIAL_COMBINATIONS = 256
MAX_PLANS_PER_RECIPE = 32
MAX_AUTHORIZED_CATALOGS_CONSIDERED = 16
# 3B.3b assembly bounds (bounded frontier search — never greedy, never unbounded):
MAX_BRIDGES_PER_PLAN = 2
MAX_REALIZATIONS_PER_HOP = 4
MAX_PHYSICAL_PATHS_PER_BINDING = 16
MAX_STATES_EXPANDED_PER_BINDING = 512


class ReplayStrength(StrEnum):
    strong = "strong"
    conditional = "conditional"
    audit_only = "audit_only"
    none = "none"


class CatalogStateStampKind(StrEnum):
    drift_watermark = "drift_watermark"


class CatalogOmissionReason(StrEnum):
    no_usable_state_stamp = "no_usable_state_stamp"
    catalog_consideration_bound = "catalog_consideration_bound"


class BindingSafety(StrEnum):
    safe = "safe"
    unsafe = "unsafe"
    not_evaluated = "not_evaluated"


class BindingQuality(StrEnum):
    exact_concept = "exact_concept"
    grain_and_role_fit = "grain_and_role_fit"
    entity_tagged = "entity_tagged"
    weak = "weak"


class SegmentKind(StrEnum):
    direct_catalog = "direct_catalog"
    intra_catalog_realization = "intra_catalog_realization"   # reserved 3B.3b
    governed_bridge = "governed_bridge"                       # reserved 3B.3b
    semantic_rollup = "semantic_rollup"                       # reserved 3B.3b


class PlanTier(StrEnum):
    tier_1_single_catalog = "tier_1_single_catalog"
    tier_2_one_bridge = "tier_2_one_bridge"
    tier_3_multi_bridge = "tier_3_multi_bridge"


class PlanResolutionStatus(StrEnum):
    resolved = "resolved"
    resolved_with_ambiguity = "resolved_with_ambiguity"
    partially_resolved = "partially_resolved"
    unresolved = "unresolved"
    safety_rejected = "safety_rejected"
    not_applicable = "not_applicable"
    bounded_out = "bounded_out"
    internal_error = "internal_error"


class PathResolutionStatus(StrEnum):
    """How far the plan's PATH is resolved — orthogonal to tier (bridge count) and to
    PlanResolutionStatus (ingredient binding). A 3B.3a tier-1 plan is ingredient_binding_only
    until the 3B.3b assembler enriches it into an executable source-to-target path."""
    ingredient_binding_only = "ingredient_binding_only"
    source_to_target_resolved = "source_to_target_resolved"
    source_to_target_rejected = "source_to_target_rejected"


class CandidateRole(StrEnum):
    # `unranked` is the neutral state for a plan the cross-catalog classifier (rank_and_classify)
    # never processed — e.g. tier-1 single-catalog plans, which only pass through the tier-1
    # ranker (order_plans) and are never assigned selected/alternative/rejected.
    unranked = "unranked"
    selected = "selected"
    equal_rank_alternative = "equal_rank_alternative"
    lower_rank_alternative = "lower_rank_alternative"
    rejected = "rejected"


class DeclarationStatus(StrEnum):
    """3B.3c — the freshness-FREE declaration outcome (third status axis, identity-bearing half).

    Derived purely from the declaration checks (connectivity, aggregation, temporal, safety);
    hashed into contract_id. Freshness deliberately has NO member here: a stale-but-fully-declared
    plan keeps declaration_status=resolved and the SAME contract_id (F7)."""
    not_compiled = "not_compiled"
    resolved = "resolved"
    unresolved_ingredient_connectivity = "unresolved_ingredient_connectivity"
    unresolved_aggregation_declaration = "unresolved_aggregation_declaration"
    unresolved_temporal_declaration = "unresolved_temporal_declaration"
    unresolved_safety_evaluation = "unresolved_safety_evaluation"
    safety_rejected = "safety_rejected"


class ContractResolutionStatus(StrEnum):
    """3B.3c — the full OBSERVED contract outcome: every DeclarationStatus member PLUS
    unresolved_freshness (the one observation-time, non-identity-bearing failure)."""
    not_compiled = "not_compiled"
    resolved = "resolved"
    unresolved_ingredient_connectivity = "unresolved_ingredient_connectivity"
    unresolved_aggregation_declaration = "unresolved_aggregation_declaration"
    unresolved_temporal_declaration = "unresolved_temporal_declaration"
    unresolved_safety_evaluation = "unresolved_safety_evaluation"
    safety_rejected = "safety_rejected"
    unresolved_freshness = "unresolved_freshness"


class AggregationFunction(StrEnum):
    """Functions a recipe/registry may DECLARE (validate, never fabricate — the compiler's only
    auto-derivations are the two SUM rules versioned by AGGREGATION_RULE_VERSION)."""
    sum = "sum"
    count = "count"     # type: ignore[assignment]  # deliberately shadows str.count on this StrEnum
    min = "min"
    max = "max"
    weighted_average = "weighted_average"
    ratio_recompute = "ratio_recompute"
    take_latest = "take_latest"


class AggregationValidation(StrEnum):
    sound = "sound"
    incompatible = "incompatible"
    undeclared = "undeclared"
    inputs_missing = "inputs_missing"


class AdditivityClass(StrEnum):
    additive = "additive"
    semi_additive = "semi_additive"
    non_additive = "non_additive"
    not_applicable = "not_applicable"
    unknown = "unknown"


class AdditivitySource(StrEnum):
    uploaded_column = "uploaded_column"
    concept = "concept"
    unknown = "unknown"


class AggregationAxisKind(StrEnum):
    entity = "entity"
    time = "time"


class ColumnRole(StrEnum):
    """Why a physical column is read — multi-role (a column may be ingredient AND join_key)."""
    ingredient = "ingredient"
    temporal_anchor = "temporal_anchor"
    join_key = "join_key"
    bridge_key = "bridge_key"
    aggregation_weight = "aggregation_weight"
    aggregation_component = "aggregation_component"
    filter = "filter"
    partition = "partition"     # type: ignore[assignment]  # deliberately shadows str.partition


class ReplayFreshness(StrEnum):
    """Replay-time comparison of a stored plan's stamps vs current state — COMPUTED by 3B.4;
    3B.3c only defines the vocabulary alongside the compile-time stamps it will compare."""
    current = "current"
    drifted = "drifted"
    unverifiable = "unverifiable"


class StampConsistency(StrEnum):
    """Did the participating catalogs' state stamps hold from scope-start to compile-end?
    (F10 revalidation; consistent only when every fingerprint recheck passed.)"""
    consistent = "consistent"
    unverifiable = "unverifiable"


def to_additivity_class(s: str | None) -> AdditivityClass:
    """Normalize a raw additivity string (uploaded column / concept registry) — NEVER raises.
    None/''/'n/a' mean the measure does not aggregate (not_applicable); anything unrecognized is
    honestly `unknown`, which downstream is NEVER treated as additive (no silent SUM)."""
    if s is None:
        return AdditivityClass.not_applicable
    v = s.strip().lower()
    if v in ("", "n/a"):
        return AdditivityClass.not_applicable
    try:
        return AdditivityClass(v)
    except ValueError:
        return AdditivityClass.unknown


class ReasonCode(StrEnum):
    selected_best_single_catalog = "selected_best_single_catalog"
    ambiguous_multiple_equal_plans = "ambiguous_multiple_equal_plans"
    no_role_compatible_column = "no_role_compatible_column"
    concept_mismatch = "concept_mismatch"
    grain_incompatible = "grain_incompatible"
    binding_safety_rejected = "binding_safety_rejected"
    missing_required_need = "missing_required_need"
    catalog_missing_target_entity = "catalog_missing_target_entity"
    no_authorized_catalog = "no_authorized_catalog"
    catalog_omitted_no_state_stamp = "catalog_omitted_no_state_stamp"
    bounded_out_max_candidate_columns = "bounded_out_max_candidate_columns"
    bounded_out_max_combinations = "bounded_out_max_combinations"
    bounded_out_max_plans = "bounded_out_max_plans"
    bounded_out_max_catalogs = "bounded_out_max_catalogs"
    planner_internal_error = "planner_internal_error"
    # reserved 3B.3c
    missing_required_aggregation = "missing_required_aggregation"
    missing_temporal_declaration = "missing_temporal_declaration"
    freshness_requirement_unsatisfied = "freshness_requirement_unsatisfied"
    # live as of 3B.3b (reserved in 3B.3a)
    ambiguous_equal_cross_catalog_paths = "ambiguous_equal_cross_catalog_paths"
    unsanctioned_bridge = "unsanctioned_bridge"
    missing_realization = "missing_realization"
    # 3B.3b assembly
    unsupported_multi_grain_ingredients = "unsupported_multi_grain_ingredients"
    ambiguous_semantic_path = "ambiguous_semantic_path"
    bounded_out_max_bridges = "bounded_out_max_bridges"
    bounded_out_max_realizations_per_hop = "bounded_out_max_realizations_per_hop"
    bounded_out_max_frontier_states = "bounded_out_max_frontier_states"
    # 3B.3c contract compiler — connectivity (C2)
    ingredient_not_connected_to_path = "ingredient_not_connected_to_path"
    # 3B.3c — aggregation declaration/validation (C4/C5)
    aggregation_strategy_missing = "aggregation_strategy_missing"
    aggregation_incompatible_with_additivity = "aggregation_incompatible_with_additivity"
    aggregation_weight_missing = "aggregation_weight_missing"
    aggregation_components_missing = "aggregation_components_missing"
    aggregation_axis_unsupported = "aggregation_axis_unsupported"
    aggregation_composition_unsupported = "aggregation_composition_unsupported"
    semi_additive_temporal_strategy_missing = "semi_additive_temporal_strategy_missing"
    additivity_source_conflict = "additivity_source_conflict"
    physical_cardinality_unavailable = "physical_cardinality_unavailable"
    # 3B.3c — temporal declaration (C3)
    temporal_anchor_missing = "temporal_anchor_missing"
    temporal_anchor_ambiguous = "temporal_anchor_ambiguous"
    # 3B.3c — universal safety over the physical read set (C6)
    safety_evaluation_incomplete = "safety_evaluation_incomplete"
    leakage_anchor_read = "leakage_anchor_read"
    protected_attribute_read = "protected_attribute_read"
    # 3B.3c — freshness observation (C7; NEVER hashed into contract_id) + run budget (C8)
    freshness_stamp_unavailable = "freshness_stamp_unavailable"
    participating_catalog_stale = "participating_catalog_stale"
    projection_lagging = "projection_lagging"
    catalog_mutated_during_compile = "catalog_mutated_during_compile"
    compile_budget_exhausted = "compile_budget_exhausted"


class GroundTemplateDiffOutcome(StrEnum):
    live_binding_present_and_ranked_first = "live_binding_present_and_ranked_first"
    live_binding_present_but_ranked_lower = "live_binding_present_but_ranked_lower"
    live_binding_absent_due_to_new_constraint = "live_binding_absent_due_to_new_constraint"
    live_binding_absent_unexpectedly = "live_binding_absent_unexpectedly"
    live_path_had_no_binding = "live_path_had_no_binding"
    not_compared = "not_compared"


@dataclass(frozen=True, slots=True)
class CatalogStateStampV1:
    catalog_source: str
    head_seq: int
    last_completed_at: str
    stamp_kind: CatalogStateStampKind = CatalogStateStampKind.drift_watermark


@dataclass(frozen=True, slots=True)
class OmittedCatalogV1:
    catalog_source: str
    reason: CatalogOmissionReason


@dataclass(frozen=True, slots=True)
class CatalogScopeV1:
    scope_id: str
    authorized_catalog_sources: tuple[str, ...]
    catalog_state_stamps: tuple[CatalogStateStampV1, ...]
    omitted_catalog_sources: tuple[OmittedCatalogV1, ...]
    read_scope_policy_version: str
    role_resolution_version: str
    resolved_at: str
    catalog_consideration_truncated: bool


@dataclass(frozen=True, slots=True)
class IngredientCandidateV1:
    recipe_id: str
    need_role: str
    concept: str
    required_grains: tuple[str, ...]
    join_role: str
    temporal_role: str
    catalog_source: str
    object_ref: str
    actual_source_grain: str | None
    binding_quality: BindingQuality
    eligible: bool
    safety: BindingSafety
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class IngredientBindingV1:
    recipe_id: str
    need_role: str
    concept: str
    required_grains: tuple[str, ...]
    join_role: str
    temporal_role: str
    bound_catalog_source: str
    bound_object_ref: str
    actual_source_grain: str | None
    binding_quality: BindingQuality
    safety: BindingSafety
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class BindingPathSegmentV1:
    segment_kind: SegmentKind
    catalog_source: str
    from_entity: str | None = None
    to_entity: str | None = None
    realization_ref: str | None = None
    bridge_fact_key: str | None = None
    cardinality: str | None = None
    direction: str | None = None
    reason_codes: tuple[ReasonCode, ...] = ()
    # 3B.3c audit evidence (F9): the semantic hop this segment realizes — populated by the
    # assembler at emission (a later 3B.3c task); None on pre-3B.3c and non-hop segments.
    relationship_id: str | None = None
    relationship_version: str | None = None


# ---- 3B.3c contract-compiler evidence (computed onto the plan; persisted only in 3B.4) ----

@dataclass(frozen=True, slots=True)
class AdditivityProvenanceV1:
    """F6 — where an ingredient's additivity came from; both raw values kept so a
    conflict (uploaded vs concept) is auditable, never silently resolved."""
    uploaded_value: str | None
    concept_value: str | None
    selected: AdditivityClass
    source: AdditivitySource
    conflict: bool


@dataclass(frozen=True, slots=True)
class IngredientAggregationV1:
    """One ingredient's aggregation stage at one hop (per hop x ingredient — a single hop may
    need SUM on two different ingredients)."""
    need_role: str
    bound_object_ref: str
    additivity: AdditivityClass
    provenance: AdditivityProvenanceV1
    physical_cardinality: Cardinality | None
    axis: AggregationAxisKind
    declared_function: AggregationFunction | None
    validation: AggregationValidation
    missing_inputs: tuple[str, ...]
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class HopAggregationV1:
    """The aggregation evidence for ONE fan-in hop of the plan's path: where it executes,
    the REALIZATION-level cardinality (F8 — not the semantic hop's), the GROUP-BY keys, and
    the per-ingredient stages."""
    semantic_hop_index: int
    segment_index: int
    from_entity: str
    to_entity: str
    execution_catalog: str
    execution_table: str
    physical_cardinality: Cardinality | None
    cardinality_source: str
    grouping_keys: tuple[str, ...]
    ingredient_stages: tuple[IngredientAggregationV1, ...]


@dataclass(frozen=True, slots=True)
class WindowSpecV1:
    """A TYPED window declaration (never a bare string)."""
    length: int | None
    unit: str | None
    boundary: str | None
    inclusive: bool


@dataclass(frozen=True, slots=True)
class ParamBindingV1:
    """The representative param instantiation the contract was compiled against (F7):
    canonical (name, value) pairs + the honest flag that these are representatives, not
    a full parameter-space validation."""
    values: tuple[tuple[str, str], ...]
    is_representative: bool


@dataclass(frozen=True, slots=True)
class TemporalDeclarationV1:
    pit_anchor: str | None
    anchor_binding: str | None
    window: WindowSpecV1 | None
    param_binding: ParamBindingV1
    time_axis_aggregating: bool
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class PhysicalColumnReadV1:
    """One physically-read column with multi-role provenance. `not_evaluated` here is
    STRUCTURAL (no resolvable _Col) and is never treated as safe."""
    object_ref: str
    catalog_source: str
    roles: tuple[ColumnRole, ...]
    safety: BindingSafety
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True, slots=True)
class PhysicalReadSetV1:
    """The immutable inventory of EVERY column the contract would read (ingredients + join/bridge
    keys + anchors + weights) — the universal-safety surface, not just the ingredient bindings."""
    columns: tuple[PhysicalColumnReadV1, ...]


@dataclass(frozen=True, slots=True)
class BindingPlanV1:
    # renamed from plan_id in 3B.3c (F11): minted over the PHYSICAL path under the frozen
    # PHYSICAL_PLAN_VERSION; the ranking tie-break; IMMUTABLE through contract compilation.
    physical_plan_id: str
    recipe_id: str
    target_entity: str | None
    tier: PlanTier
    catalog_source: str
    ingredient_bindings: tuple[IngredientBindingV1, ...]
    path_segments: tuple[BindingPathSegmentV1, ...]
    resolution_status: PlanResolutionStatus
    primary_reason_code: ReasonCode | None
    reason_codes: tuple[ReasonCode, ...]
    safety: BindingSafety
    preference_rank: int
    preference_reasons: tuple[str, ...]
    # 3B.3b — derived by make_binding_plan (the canonical constructor), deliberately no defaults:
    participating_catalogs: tuple[str, ...]
    bridge_count: int
    path_resolution_status: PathResolutionStatus
    candidate_role: CandidateRole
    # 3B.3c — the contract axes + evidence, filled by the shadow compiler (compute-only; defaults
    # keep every pre-3B.3c constructor working). Diagnostics live ONLY in contract_* fields —
    # never mixed into the ingredient/path axes' reason fields above.
    contract_id: str | None = None
    declaration_status: DeclarationStatus = DeclarationStatus.not_compiled
    contract_resolution_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled
    contract_primary_reason_code: ReasonCode | None = None
    contract_reason_codes: tuple[ReasonCode, ...] = ()
    hop_aggregations: tuple[HopAggregationV1, ...] = ()
    temporal_declaration: TemporalDeclarationV1 | None = None
    physical_read_set: PhysicalReadSetV1 | None = None
    audit_envelope: PlannerReplayEnvelopeV1 | None = None
    resolved_at_compilation: datetime | None = None


@dataclass(frozen=True, slots=True)
class BoundingMetricsV1:
    candidate_columns_truncated: bool
    combinations_truncated: bool
    plans_truncated: bool
    catalog_consideration_truncated: bool
    total_candidate_columns_considered: int
    total_combinations_explored: int
    total_plans_preserved: int
    # 3B.3b assembly bounds observability:
    realizations_truncated: bool
    bridge_transitions_truncated: bool
    frontier_states_truncated: bool
    deeper_tiers_not_explored: bool
    total_states_expanded: int
    total_bridge_transitions_explored: int


@dataclass(frozen=True, slots=True)
class GroundTemplateDiffV1:
    outcome: GroundTemplateDiffOutcome
    live_bound_object_refs: tuple[str, ...]
    planner_matched_plan_id: str | None


@dataclass(frozen=True, slots=True)
class PlannerReplayEnvelopeV1:
    planner_version: str
    reason_code_registry_version: str
    applicability_mapping_version: str
    recipe_registry_version: str
    need_metadata_version: str
    graph_version: str
    realization_derivation_version: str
    bridge_derivation_version: str
    concept_registry_version: str
    catalog_scope: CatalogScopeV1
    replay_strength: ReplayStrength
    planner_input_hash: str
    # 3B.3b — the exact governed crossings visible to this run + the plan schema they were minted under:
    active_bridge_fact_keys: tuple[str, ...]
    plan_contract_version: str
    # 3B.3c (F9) — the full audit version set + compile-time state evidence. Defaults keep
    # pre-compile envelopes constructible; compiled plans pin all of it and set
    # replay_strength=ReplayStrength.audit_only (watermarks correlate drift; they never permit
    # deterministic re-execution). stamp_consistency defaults to `unverifiable` (fail-closed):
    # only the compile-end fingerprint recheck may claim `consistent`.
    aggregation_rule_version: str = AGGREGATION_RULE_VERSION
    additivity_rule_version: str = ADDITIVITY_RULE_VERSION
    temporal_rule_version: str = TEMPORAL_RULE_VERSION
    safety_evaluator_version: str = SAFETY_EVALUATOR_VERSION
    drift_freshness_sla_version: str = DRIFT_FRESHNESS_SLA_VERSION
    planner_bounds_version: str = PLANNER_BOUNDS_VERSION
    ranking_version: str = RANKING_VERSION
    authz_role_claims: tuple[str, ...] = ()
    recipe_content_hash: str = ""
    catalog_state_stamps: tuple[CatalogStateStampV1, ...] = ()
    stamp_consistency: StampConsistency = StampConsistency.unverifiable


@dataclass(frozen=True, slots=True)
class BindingPlanningResultV1:
    run_id: str | None
    recipe_id: str
    target_entity: str | None
    catalog_scope_id: str
    selected_plan_id: str | None
    candidate_plans: tuple[BindingPlanV1, ...]
    result_status: PlanResolutionStatus
    primary_reason_code: ReasonCode | None
    reason_codes: tuple[ReasonCode, ...]
    bounding: BoundingMetricsV1
    ground_template_diff: GroundTemplateDiffV1
    replay_envelope: PlannerReplayEnvelopeV1
    # 3B.3c — the contract-axis roll-up across the run's COMPILED (source_to_target_resolved)
    # plans; orthogonal to result_status/selected_plan_id (the ingredient axis), which are
    # untouched. selected_contract_physical_plan_id carries a physical id; selected_contract_id
    # its declaration identity.
    contract_result_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled
    selected_contract_physical_plan_id: str | None = None
    selected_contract_id: str | None = None


def tier_from_bridge_count(n: int) -> PlanTier:
    if n == 0:
        return PlanTier.tier_1_single_catalog
    if n == 1:
        return PlanTier.tier_2_one_bridge
    return PlanTier.tier_3_multi_bridge


def make_binding_plan(*, recipe_id: str, target_entity: str | None, catalog_source: str,
                      ingredient_bindings: tuple[IngredientBindingV1, ...],
                      path_segments: tuple[BindingPathSegmentV1, ...],
                      resolution_status: PlanResolutionStatus,
                      path_resolution_status: PathResolutionStatus,
                      primary_reason_code: ReasonCode | None,
                      reason_codes: tuple[ReasonCode, ...],
                      safety: BindingSafety,
                      preference_rank: int,
                      preference_reasons: tuple[str, ...],
                      candidate_role: CandidateRole) -> BindingPlanV1:
    """The ONE canonical constructor: derives participating_catalogs (ordered by first traversal, dedup,
    catalog_source first), bridge_count, tier, and a physical_plan_id over the canonical content + the
    FROZEN PHYSICAL_PLAN_VERSION (never PLAN_CONTRACT_VERSION — F1: schema bumps must not move ids);
    validates the structural invariants. participating_catalogs cannot be a static default (it depends on
    catalog_source + segments), which is why this constructor exists."""
    participating: list[str] = [catalog_source]
    for seg in path_segments:
        if seg.catalog_source not in participating:
            participating.append(seg.catalog_source)
    bridge_count = sum(1 for s in path_segments if s.segment_kind is SegmentKind.governed_bridge)
    tier = tier_from_bridge_count(bridge_count)
    # structural validation — participating_catalogs/bridge_count are DERIVED here (single source of truth,
    # so they can't drift), leaving one meaningful fail-closed invariant: the bridge budget. A plan that
    # exceeds MAX_BRIDGES_PER_PLAN must NEVER be constructed (the assembler bounds this, but the canonical
    # constructor enforces it so no caller can mint an over-budget plan).
    if bridge_count > MAX_BRIDGES_PER_PLAN:
        raise ValueError(f"bridge_count {bridge_count} exceeds MAX_BRIDGES_PER_PLAN {MAX_BRIDGES_PER_PLAN}")
    if path_resolution_status is PathResolutionStatus.source_to_target_resolved \
            and resolution_status is PlanResolutionStatus.unresolved:
        raise ValueError("source_to_target_resolved plan cannot have resolution_status=unresolved")
    refs = tuple(sorted(b.bound_object_ref for b in ingredient_bindings))
    segments_material = ">".join(
        f"{s.segment_kind}:{s.catalog_source}:{s.realization_ref or s.bridge_fact_key or ''}"
        for s in path_segments)
    # path_resolution_status is part of the hashed material: a tier-1 resolved plan and an
    # immediate-dead-end reject over the same refs/segments must NOT share a physical_plan_id
    # (3B.4 keys its store by physical id). It is stable at construction time — the ranker only
    # rewrites resolution_status/candidate_role — so it is safe to hash (candidate_role is NOT:
    # it is reset post-construction via dataclasses.replace). The tail is the FROZEN
    # PHYSICAL_PLAN_VERSION: byte-identical to the pre-split material, so every 3B.3b id is stable.
    material = (f"{recipe_id}|{catalog_source}|{'|'.join(refs)}|{tier}|{segments_material}"
                f"|{path_resolution_status}|{PLANNER_VERSION}|{PHYSICAL_PLAN_VERSION}")
    physical_plan_id = "bp_" + hashlib.sha256(material.encode()).hexdigest()[:16]
    return BindingPlanV1(
        physical_plan_id=physical_plan_id, recipe_id=recipe_id, target_entity=target_entity, tier=tier,
        catalog_source=catalog_source, ingredient_bindings=ingredient_bindings, path_segments=path_segments,
        resolution_status=resolution_status, primary_reason_code=primary_reason_code, reason_codes=reason_codes,
        safety=safety, preference_rank=preference_rank, preference_reasons=preference_reasons,
        participating_catalogs=tuple(participating), bridge_count=bridge_count,
        path_resolution_status=path_resolution_status, candidate_role=candidate_role)


# Observation-time reason codes (freshness + run budget): they describe WHEN/HOW the compile ran,
# not WHAT was declared — excluded from the contract_id material so a stale recompile of identical
# declarations keeps its identity (F7).
_NON_DECLARATION_REASON_CODES = frozenset({
    ReasonCode.freshness_requirement_unsatisfied,
    ReasonCode.freshness_stamp_unavailable,
    ReasonCode.participating_catalog_stale,
    ReasonCode.projection_lagging,
    ReasonCode.catalog_mutated_during_compile,
    ReasonCode.compile_budget_exhausted,
})

_REASON_CODE_ORDER = {rc: i for i, rc in enumerate(ReasonCode)}


def canonical_reason_codes(codes: Iterable[ReasonCode]) -> tuple[ReasonCode, ...]:
    """Canonical diagnostic order: ReasonCode DEFINITION (registry) order, deduped — so no
    serialization or accumulation order can perturb hashed material or stored diagnostics."""
    return tuple(sorted(set(codes), key=_REASON_CODE_ORDER.__getitem__))


def make_contract_id(plan: BindingPlanV1, *, resolved_at_compilation: datetime) -> str:
    """The declaration identity (F7/F12), minted over WHAT was declared: declaration_status +
    the DECLARATION reason codes + the per-ingredient aggregation declarations + the temporal
    signature + the compiler rule versions, anchored to the immutable physical_plan_id and the
    PLAN_CONTRACT_VERSION schema.

    DELIBERATE exclusions — the freshness observation never enters the identity: the
    contract_resolution_status freshness delta, every freshness/budget reason code, catalog state
    stamps, and `resolved_at_compilation` (accepted here precisely so no caller can believe the
    compile time participates: it is carried as plan evidence, NEVER hashed). Identical
    declarations therefore compile to the SAME contract_id, fresh or stale, today or in replay."""
    del resolved_at_compilation     # evidence, not identity — see docstring
    decl_codes = canonical_reason_codes(
        rc for rc in plan.contract_reason_codes if rc not in _NON_DECLARATION_REASON_CODES)
    stages = sorted(
        (s.need_role, s.additivity.value,
         s.declared_function.value if s.declared_function is not None else "",
         s.validation.value)
        for h in plan.hop_aggregations for s in h.ingredient_stages)
    stage_material = ";".join(",".join(s) for s in stages)
    td = plan.temporal_declaration
    if td is None:
        temporal_material = ""
    else:
        w = td.window
        window_material = "" if w is None else f"{w.length}:{w.unit}:{w.boundary}:{w.inclusive}"
        temporal_material = f"{td.pit_anchor or ''}~{window_material}~{td.time_axis_aggregating}"
    material = (f"{plan.physical_plan_id}|{plan.declaration_status}|{'|'.join(decl_codes)}"
                f"|{stage_material}|{temporal_material}"
                f"|{AGGREGATION_RULE_VERSION}|{ADDITIVITY_RULE_VERSION}|{TEMPORAL_RULE_VERSION}"
                f"|{SAFETY_EVALUATOR_VERSION}|{PLAN_CONTRACT_VERSION}")
    return "cc_" + hashlib.sha256(material.encode()).hexdigest()[:16]
