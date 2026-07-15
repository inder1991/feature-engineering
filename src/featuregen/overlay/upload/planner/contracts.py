"""Phase-3B.3a — cross-catalog binding planner contracts + reason-code/status vocabulary.

The BACKBONE reused by 3B.3b/c/3B.4/3C. Supersedes the parent 3B design's CrossCatalog* names.
Frozen dataclasses; lowercase snake_case StrEnum values. No behaviour — pure contracts + constants
(plus the pure canonical `make_binding_plan` constructor added in 3B.3b — the ONE plan-id authority)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

PLANNER_VERSION = "3b3a.1.0.0"
# The BindingPlanV1 schema/derivation version: hashed into every plan_id so plans minted under a
# different contract shape can never collide with (or masquerade as) plans from another version.
PLAN_CONTRACT_VERSION = "3b3b.1.0.0"
REASON_CODE_REGISTRY_VERSION = "1.1.0"
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
    selected = "selected"
    equal_rank_alternative = "equal_rank_alternative"
    lower_rank_alternative = "lower_rank_alternative"
    rejected = "rejected"


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
    # 3B.3b — derived by make_binding_plan (the canonical constructor), deliberately no defaults:
    participating_catalogs: tuple[str, ...]
    bridge_count: int
    path_resolution_status: PathResolutionStatus
    candidate_role: CandidateRole


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
    catalog_source first), bridge_count, tier, and a plan_id over the canonical content + PLAN_CONTRACT_VERSION;
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
    # immediate-dead-end reject over the same refs/segments must NOT share a plan_id (3B.4 keys
    # its store by plan_id). It is stable at construction time — the ranker only rewrites
    # resolution_status/candidate_role — so it is safe to hash (candidate_role is NOT: it is
    # reset post-construction via dataclasses.replace).
    material = (f"{recipe_id}|{catalog_source}|{'|'.join(refs)}|{tier}|{segments_material}"
                f"|{path_resolution_status}|{PLANNER_VERSION}|{PLAN_CONTRACT_VERSION}")
    plan_id = "bp_" + hashlib.sha256(material.encode()).hexdigest()[:16]
    return BindingPlanV1(
        plan_id=plan_id, recipe_id=recipe_id, target_entity=target_entity, tier=tier,
        catalog_source=catalog_source, ingredient_bindings=ingredient_bindings, path_segments=path_segments,
        resolution_status=resolution_status, primary_reason_code=primary_reason_code, reason_codes=reason_codes,
        safety=safety, preference_rank=preference_rank, preference_reasons=preference_reasons,
        participating_catalogs=tuple(participating), bridge_count=bridge_count,
        path_resolution_status=path_resolution_status, candidate_role=candidate_role)
