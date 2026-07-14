"""Phase-3B.3a — cross-catalog binding planner contracts + reason-code/status vocabulary.

The BACKBONE reused by 3B.3b/c/3B.4/3C. Supersedes the parent 3B design's CrossCatalog* names.
Frozen dataclasses; lowercase snake_case StrEnum values. No behaviour — pure contracts + constants."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PLANNER_VERSION = "3b3a.1.0.0"
REASON_CODE_REGISTRY_VERSION = "1.0.0"
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


class PlanResolutionStatus(StrEnum):
    resolved = "resolved"
    partially_resolved = "partially_resolved"
    unresolved = "unresolved"
    safety_rejected = "safety_rejected"
    not_applicable = "not_applicable"
    bounded_out = "bounded_out"
    internal_error = "internal_error"


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
    # reserved 3B.3b
    ambiguous_equal_cross_catalog_paths = "ambiguous_equal_cross_catalog_paths"
    unsanctioned_bridge = "unsanctioned_bridge"
    missing_realization = "missing_realization"


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


@dataclass(frozen=True, slots=True)
class BindingPlanV1:
    plan_id: str
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


@dataclass(frozen=True, slots=True)
class BoundingMetricsV1:
    candidate_columns_truncated: bool
    combinations_truncated: bool
    plans_truncated: bool
    catalog_consideration_truncated: bool
    total_candidate_columns_considered: int
    total_combinations_explored: int
    total_plans_preserved: int


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
