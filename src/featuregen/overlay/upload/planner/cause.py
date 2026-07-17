"""Phase-3B.4 — the two-layer resolution-cause taxonomy.

Whether an unresolved reason is *expected*, *unsupported topology*, or a *classifier defect* cannot be
inferred from the reason code alone (a ``binding_safety_rejected`` can be a CORRECT hard block;
``ingredient_not_connected_to_path`` can be a genuinely unsupported topology). So cause-labelling has
TWO layers:

  * Layer A — a static reason CATEGORY (``ReasonCategory``): a versioned map, MACHINE-computed, and
    EXHAUSTIVE over the whole ``ReasonCode`` registry. A new code with no entry -> ``operationally_unmeasured``.
  * Layer B — the contextual cause (``ResolutionCause``): per observed shape, from EVIDENCE + an EXPERT
    label. ``classifier_defect`` is a Layer-B determination, never derived from the code.

``operationally_unmeasured`` (a registry-map gap) is distinct from ``unknown`` (mapped, but Layer-B
unclassified pending an expert label). The 3C gate requires zero of BOTH, plus zero ``classifier_defect``.
"""
from __future__ import annotations

from enum import StrEnum

from featuregen.overlay.upload.planner.contracts import ReasonCode

CATEGORY_MAP_VERSION = "1.0.0"


class ReasonCategory(StrEnum):
    missing_authoring = "missing_authoring"            # a declaration a later authoring phase supplies
    policy_or_catalog_state = "policy_or_catalog_state"  # safety block / freshness / catalog availability
    topology_or_model = "topology_or_model"            # the recipe/catalog structure can't support the contract
    bounding = "bounding"                              # a planner bound was hit (truncation)
    selection = "selection"                            # a positive selection / ambiguity marker (not a failure)
    internal = "internal"                              # a planner internal error


class ResolutionCause(StrEnum):
    expected = "expected"                              # a legitimate expected outcome (authoring/policy/topology)
    unsupported_topology = "unsupported_topology"      # a genuinely unsupported shape (correct rejection)
    classifier_defect = "classifier_defect"            # a modelling/logic bug (Layer-B, expert)
    operationally_unmeasured = "operationally_unmeasured"  # a ReasonCode with no Layer-A map entry
    unknown = "unknown"                                # mapped, but no Layer-B expert label yet


# Layer A — EXHAUSTIVE over ReasonCode. `assert_map_exhaustive` (+ the static test) fails if a member is
# unmapped, so a newly-shipped reason code forces a taxonomy decision before the gate can pass.
RESOLUTION_CATEGORY_MAP: dict[ReasonCode, ReasonCategory] = {
    # missing_authoring — a declaration a later authoring phase supplies
    ReasonCode.aggregation_strategy_missing: ReasonCategory.missing_authoring,
    ReasonCode.aggregation_weight_missing: ReasonCategory.missing_authoring,
    ReasonCode.aggregation_components_missing: ReasonCategory.missing_authoring,
    ReasonCode.aggregation_ordering_column_missing: ReasonCategory.missing_authoring,
    ReasonCode.semi_additive_temporal_strategy_missing: ReasonCategory.missing_authoring,
    ReasonCode.missing_required_aggregation: ReasonCategory.missing_authoring,
    ReasonCode.missing_temporal_declaration: ReasonCategory.missing_authoring,
    ReasonCode.temporal_anchor_missing: ReasonCategory.missing_authoring,
    # policy_or_catalog_state — safety block / freshness / catalog availability
    ReasonCode.binding_safety_rejected: ReasonCategory.policy_or_catalog_state,
    ReasonCode.leakage_anchor_read: ReasonCategory.policy_or_catalog_state,
    ReasonCode.protected_attribute_read: ReasonCategory.policy_or_catalog_state,
    ReasonCode.safety_evaluation_incomplete: ReasonCategory.policy_or_catalog_state,
    ReasonCode.freshness_requirement_unsatisfied: ReasonCategory.policy_or_catalog_state,
    ReasonCode.freshness_stamp_unavailable: ReasonCategory.policy_or_catalog_state,
    ReasonCode.participating_catalog_stale: ReasonCategory.policy_or_catalog_state,
    ReasonCode.projection_lagging: ReasonCategory.policy_or_catalog_state,
    ReasonCode.catalog_mutated_during_compile: ReasonCategory.policy_or_catalog_state,
    ReasonCode.catalog_omitted_no_state_stamp: ReasonCategory.policy_or_catalog_state,
    ReasonCode.catalog_missing_target_entity: ReasonCategory.policy_or_catalog_state,
    ReasonCode.no_authorized_catalog: ReasonCategory.policy_or_catalog_state,
    # topology_or_model — the recipe/catalog structure can't support the contract
    ReasonCode.ingredient_not_connected_to_path: ReasonCategory.topology_or_model,
    ReasonCode.physical_cardinality_unavailable: ReasonCategory.topology_or_model,
    ReasonCode.aggregation_composition_unsupported: ReasonCategory.topology_or_model,
    ReasonCode.aggregation_axis_unsupported: ReasonCategory.topology_or_model,
    ReasonCode.aggregation_incompatible_with_additivity: ReasonCategory.topology_or_model,
    ReasonCode.additivity_source_conflict: ReasonCategory.topology_or_model,
    ReasonCode.unsupported_multi_grain_ingredients: ReasonCategory.topology_or_model,
    ReasonCode.missing_realization: ReasonCategory.topology_or_model,
    ReasonCode.unsanctioned_bridge: ReasonCategory.topology_or_model,
    ReasonCode.temporal_anchor_ambiguous: ReasonCategory.topology_or_model,
    ReasonCode.concept_mismatch: ReasonCategory.topology_or_model,
    ReasonCode.grain_incompatible: ReasonCategory.topology_or_model,
    ReasonCode.no_role_compatible_column: ReasonCategory.topology_or_model,
    ReasonCode.missing_required_need: ReasonCategory.topology_or_model,
    # bounding — a planner bound was hit
    ReasonCode.bounded_out_max_bridges: ReasonCategory.bounding,
    ReasonCode.bounded_out_max_candidate_columns: ReasonCategory.bounding,
    ReasonCode.bounded_out_max_catalogs: ReasonCategory.bounding,
    ReasonCode.bounded_out_max_combinations: ReasonCategory.bounding,
    ReasonCode.bounded_out_max_frontier_states: ReasonCategory.bounding,
    ReasonCode.bounded_out_max_plans: ReasonCategory.bounding,
    ReasonCode.bounded_out_max_realizations_per_hop: ReasonCategory.bounding,
    ReasonCode.compile_budget_exhausted: ReasonCategory.bounding,
    # selection — positive selection / ambiguity markers (not failures)
    ReasonCode.selected_best_single_catalog: ReasonCategory.selection,
    ReasonCode.ambiguous_multiple_equal_plans: ReasonCategory.selection,
    ReasonCode.ambiguous_equal_cross_catalog_paths: ReasonCategory.selection,
    ReasonCode.ambiguous_semantic_path: ReasonCategory.selection,
    # internal
    ReasonCode.planner_internal_error: ReasonCategory.internal,
}


def category_of(reason: ReasonCode) -> ReasonCategory | None:
    """Layer A — the static category, or None if the code has no map entry (a registry gap)."""
    return RESOLUTION_CATEGORY_MAP.get(reason)


def assert_map_exhaustive() -> None:
    """Fail if any ReasonCode has no Layer-A category — a NEW code must force a taxonomy decision before
    the 3C gate can pass. Used by the static test AND the gate's population-explainability check."""
    missing = [r.value for r in ReasonCode if r not in RESOLUTION_CATEGORY_MAP]
    if missing:
        raise AssertionError(f"ReasonCode(s) unmapped in RESOLUTION_CATEGORY_MAP: {sorted(missing)}")


def contextual_cause(reason: ReasonCode, expert_label: ResolutionCause | None) -> ResolutionCause:
    """Layer B — the contextual cause. An unmapped code is `operationally_unmeasured` (regardless of a
    label); a mapped code with no expert label is `unknown`; else the expert's determination."""
    if category_of(reason) is None:
        return ResolutionCause.operationally_unmeasured
    if expert_label is None:
        return ResolutionCause.unknown
    return expert_label
