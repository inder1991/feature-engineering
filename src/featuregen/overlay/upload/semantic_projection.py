"""SE-7 — the enforced projection: assembled semantic candidates become Gate-1 candidates.

Under ``semantic_v1`` the recipe lens is SERVED from the semantic engine — frozen context →
capability binder → eligibility fold → assembly — and this module is the seam that turns an
``AssembledCandidateV1`` into the exact carriers Gate-1 already speaks (SE-10 step 6: reuse the
``FeatureIdea``/``RoleBinding``/``Requirement`` carriers; never a third role-binding structure).

The projection is a TRANSLATION, not a re-decision — every semantic verdict maps onto the
closed legacy vocabulary it corresponds to:

* Typed gauntlet requirements land as legacy ``Requirement`` objects through their EXACT
  equivalents (identifier uniqueness IS the grain-uniqueness check; event-history depth IS the
  temporal-population check) — minted through ``build_requirement`` so the registry validates
  them, with the semantic origin named in ``detail``.
* Authority-floor codes (a proposal that retrieves but never clears) are NOT external data
  checks — they become ``RoleBinding.confirmation_required``, the carrier that has always meant
  "the human must confirm this binding at Gate 1".
* A candidate the gauntlet REFUSES (bound target, blocked binding) or whose temporal contract
  did not compile is a REJECTION with its named codes — never a served card, never silently
  dropped.

Origin-honest: projected ideas carry ``generation_source="recipe"`` with the V2 recipe id, and
their role-binding authorities are the observed ``producer/strength`` pins — the UI renders
what the engine measured, not a rounded-up story.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.candidate_assembly import AssembledSetV1
from featuregen.overlay.upload.typed_gauntlet import validate_candidate

#: Typed-gauntlet requirement codes → the legacy closed REQUIREMENT_CODES vocabulary. Each row
#: is a semantic identity, not an approximation — the detail text still names the deeper check.
_REQUIREMENT_PROJECTION = {
    R.IDENTIFIER_UNIQUENESS: "GRAIN_IS_UNIQUE",
    R.EVENT_HISTORY_VERIFICATION: "TEMPORAL_IS_POPULATED",
    R.CURRENCY_POLICY_MISSING: "CURRENCY_CONSISTENT",
    R.RELATIONSHIP_REQUIRED: "JOIN_CONNECTIVITY",
}

#: Verdict codes that mean "a human confirms this binding at Gate 1" — the RoleBinding carrier's
#: own confirmation_required flag, never an external-check Requirement.
_CONFIRMATION_CODES = frozenset({R.PROPOSED_METADATA_ONLY, R.SEMANTIC_AUTHORITY_INSUFFICIENT})


@dataclass(frozen=True, slots=True)
class SemanticProjectionV1:
    """One projection pass: served ideas + ACTIONABLE options + honest refusals.

    A3 (validated finding 8): actionable candidates (blocked/ambiguous/missing with a named
    resolution) are OPTIONS now, not rejections — they project as ideas too, so they mint
    option ids and decision rows and can be SAVED as ideas while create_contract stays
    blocked. Only gauntlet-refused, temporal-uncompiled, and malformed output remain
    rejections."""

    ideas: list
    actionable_ideas: list                # undecided work, save_idea-able, never hidden
    rejections: list                      # the V1 wire shape: {name, reason, code}
    grounded_ids: frozenset
    rejected_ids: dict
    binding_by_id: dict


def _role_bindings(candidate, catalog_source: str):
    from featuregen.overlay.upload.feature_assist import RoleBinding

    eligibility = candidate.eligibility or {}
    bindings = []
    for verdict in candidate.verdicts:
        if verdict.status != "bound" or not verdict.selected_ref:
            continue
        chosen = eligibility.get((verdict.role, verdict.selected_ref))
        bindings.append(RoleBinding(
            role=verdict.role,
            ref=(catalog_source, verdict.selected_ref),
            authority=chosen.authority_observed if chosen is not None else "",
            confirmation_required=bool(_CONFIRMATION_CODES
                                       & set(verdict.reason_codes))))
    return tuple(bindings)


def _requirements(validation, catalog_source: str):
    from featuregen.overlay.upload.validation_requirements import build_requirement

    projected = []
    for requirement in validation.requirements:
        legacy_code = _REQUIREMENT_PROJECTION.get(requirement.code)
        if legacy_code is None:           # e.g. a floor code riding a bound verdict — not an
            continue                      # external check; it already set confirmation_required
        projected.append(build_requirement(
            code=legacy_code,
            operand=(catalog_source, requirement.object_ref),
            detail=f"[{requirement.code}] {requirement.detail}"))
    return tuple(projected)


def _rejection(candidate, codes, reason: str) -> dict:
    label = candidate.planning_request.output.display_label or candidate.recipe_id
    return {"name": label, "reason": reason,
            "code": codes[0] if codes else "SEMANTIC_NOT_BINDABLE"}


def _served_idea(assembled, validation, *, catalog_source: str,
                 candidate_status: str = ""):
    from featuregen.overlay.upload.feature_assist import FeatureIdea
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    candidate = assembled.candidate
    request = candidate.planning_request
    try:
        description = v2_recipe_by_id(candidate.recipe_id).business_definition
    except Exception:                     # a non-recipe primary (LLM intent fronting alone)
        description = request.conceptual_reason
    bound_refs = [v.selected_ref for v in candidate.verdicts
                  if v.status == "bound" and v.selected_ref]
    return FeatureIdea(
        name=request.output.display_label or candidate.recipe_id,
        description=description,
        derives_from=list(bound_refs),
        aggregation=None,
        grain_table=None,
        derives_pairs=tuple((catalog_source, ref) for ref in bound_refs),
        rationale=request.conceptual_reason,
        validation_status=("DESIGN_CHECKED" if validation.status == "design_checked"
                           else "NEEDS_EXTERNAL_VALIDATION"),
        requirements=_requirements(validation, catalog_source),
        generation_source="recipe",
        candidate_status=candidate_status,
        recipe_id=candidate.recipe_id,
        input_role_bindings=_role_bindings(candidate, catalog_source),
        operand_roles=tuple(sorted(
            (v.selected_ref, v.role) for v in candidate.verdicts
            if v.status == "bound" and v.selected_ref)),
    )


def project_assembled_set(assembled_set: AssembledSetV1, *, catalog_source: str,
                          target_ref: str | None = None) -> SemanticProjectionV1:
    """Serve the ranked candidates through the typed gauntlet; refuse the rest by name."""
    ideas: list = []
    rejections: list = []
    grounded: set = set()
    rejected: dict = {}
    binding_by_id: dict = {}

    for assembled in assembled_set.ranked:
        candidate = assembled.candidate
        if candidate.temporal_blocker:    # the temporal contract did not compile — setup work
            rejected[candidate.recipe_id] = (R.TEMPORAL_POLICY_UNRESOLVED,)
            rejections.append(_rejection(
                candidate, (R.TEMPORAL_POLICY_UNRESOLVED,), candidate.temporal_blocker))
            continue
        validation = validate_candidate(candidate, target_ref=target_ref)
        if validation.status == "refused":
            codes = tuple(r["code"] for r in validation.refusals)
            rejected[candidate.recipe_id] = codes
            rejections.append(_rejection(
                candidate, codes, "refused by the typed design gauntlet"))
            continue
        ideas.append(_served_idea(assembled, validation, catalog_source=catalog_source))
        grounded.add(candidate.recipe_id)
        floors = any(_CONFIRMATION_CODES & set(v.reason_codes)
                     for v in candidate.verdicts if v.status == "bound")
        binding_by_id[candidate.recipe_id] = "acceptable" if floors else "exact"

    actionable_ideas: list = []
    for assembled in assembled_set.actionable:
        candidate = assembled.candidate
        codes = tuple(dict.fromkeys(
            code for v in candidate.verdicts for code in v.reason_codes))
        resolution = next((v.resolution for v in candidate.verdicts if v.resolution),
                          "no eligible binding for every required operand")
        rejected[candidate.recipe_id] = codes or ("SEMANTIC_NOT_BINDABLE",)
        # A3: the candidate is a visible OPTION carrying its own undecided state — the named
        # resolution rides the card's critic-note-free channel (candidate_status = the honest
        # binding state; the wire section carries blockers from the activation fold).
        validation = validate_candidate(candidate)
        actionable_ideas.append(_served_idea(
            assembled, validation, catalog_source=catalog_source,
            candidate_status=candidate.binding_state))

    return SemanticProjectionV1(
        ideas=ideas, actionable_ideas=actionable_ideas, rejections=rejections,
        grounded_ids=frozenset(grounded), rejected_ids=rejected, binding_by_id=binding_by_id)


__all__ = ["SemanticProjectionV1", "project_assembled_set"]
