"""SE-7 (part 1) — the V2 recipe planning lens: authored objectives in, bound candidates out.

The hypothesis workflow's recipe source becomes the ATOMIC V2 registry, directly:

* :func:`v2_applicability` classifies every `RecipeDefinitionV2` against the confirmed scope
  from its AUTHORED `primary_objective` / `supporting_objectives` — no legacy tag bag, no
  crosswalk, no alias resolution (the hypothesis path starts from atomic ids and needs none).
  Same law as the legacy classifier: primary when the authored primary IS confirmed (or a
  descendant of one under `INCLUDE_DESCENDANTS`); supporting when only a supporting objective
  is confirmed; out of scope otherwise; unscoped fails open to all-primary. Same exactly-one
  invariant, enforced.
* :func:`v2_recipe_candidates` assembles, for each eligible recipe, the complete DATA a Gate-1
  candidate needs: the SE-1 planning request (one resolved variant) with its content hash, the
  shared binder's per-operand verdicts (the SAME `bind_v2_operands` tuple the suggestion and
  formula paths consume — never a second binding), the folded binding state, the compiled
  temporal contract (or its named blocker), the authored readiness, and the BR-23 review
  validity AT the recipe's current revision.

What this module deliberately does NOT do (part 2, the gate1 wiring): project candidates into
`FeatureIdea`, mint options, or touch the considered set. Nothing imports this module until the
`FEATUREGEN_SEMANTIC_PLANNING` mode selects it — landing it is byte-identical to today.

The Tranche-1 per-recipe column load is GONE (SE-5 full): when a frozen context is supplied,
binding runs through `bind_planning_request` — shortlists from the context's concept index,
capabilities in ONE batched read for the whole request, eligibility through SE-4's fold — and
`graph_node` is never queried per recipe. Without a context (compatibility callers), the lens
assembles one itself, once.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_contract_v2 import RecipeDefinitionV2
from featuregen.overlay.upload.recipe_operand_policy import (
    OperandBindingVerdictV1,
    bind_planning_request,
)
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope, ScopeExpansion
from featuregen.overlay.upload.taxonomy.use_cases import descendants

#: The candidate-level fold of per-operand verdicts (plan §6.7's semantic-binding axis).
BINDING_STATES = ("bound", "ambiguous", "missing", "blocked")


@dataclass(frozen=True, slots=True)
class V2ApplicabilityResult:
    """Every V2 recipe id mapped to exactly one relationship — the exactly-one invariant the
    legacy classifier enforces, re-enforced here over the authored objectives."""

    by_recipe: dict[str, str]            # recipe_id -> "primary" | "supporting" | "out_of_scope"
    eligible_ids: frozenset[str]         # primary ∪ supporting


def v2_applicability(scope: ConfirmedScope) -> V2ApplicabilityResult:
    all_ids = {recipe.recipe_id for recipe in V2_RECIPES}
    if scope.unscoped:
        return V2ApplicabilityResult(
            by_recipe={rid: "primary" for rid in all_ids}, eligible_ids=frozenset(all_ids))

    confirmed = {scope.primary, *scope.secondary} - {None}
    expanded: set[str] = set(confirmed)
    if scope.expansion is ScopeExpansion.INCLUDE_DESCENDANTS:
        for uid in confirmed:
            expanded.update(descendants(uid))

    by_recipe: dict[str, str] = {}
    for recipe in V2_RECIPES:
        if recipe.primary_objective in expanded:
            by_recipe[recipe.recipe_id] = "primary"
        elif any(objective in confirmed for objective in recipe.supporting_objectives):
            by_recipe[recipe.recipe_id] = "supporting"
        else:
            by_recipe[recipe.recipe_id] = "out_of_scope"

    if set(by_recipe) != all_ids:
        raise ValueError("v2 applicability invariant violated: classified id-set != registry")
    eligible = frozenset(rid for rid, rel in by_recipe.items() if rel != "out_of_scope")
    return V2ApplicabilityResult(by_recipe=by_recipe, eligible_ids=eligible)


def v2_applicability_as_result(scope: ConfirmedScope):
    """The V2 classification in the LEGACY ``ApplicabilityResult`` carrier — so under
    ``semantic_v1`` the disposition lens folds the universe that was actually planned (the V2
    registry), not the legacy one, without growing a second disposition path. Reason codes use
    the lens's own vocabulary (the placement is decided by authored objectives here, never by
    the legacy crosswalk)."""
    from featuregen.overlay.upload.taxonomy.applicability import ApplicabilityResult

    v2 = v2_applicability(scope)
    reasons = {"primary": ("primary_match",), "supporting": ("secondary_match",),
               "out_of_scope": ("no_confirmed_use_case_match",)}
    return ApplicabilityResult(
        by_recipe=dict(v2.by_recipe),
        eligible_ids=v2.eligible_ids,
        reason_codes={rid: reasons[rel] for rid, rel in v2.by_recipe.items()})


def fold_binding_state(verdicts: tuple[OperandBindingVerdictV1, ...],
                       definition: RecipeDefinitionV2) -> str:
    """The candidate-level state, fail-closed in severity order: any BLOCKED required operand
    blocks the candidate; else any AMBIGUOUS required operand marks it ambiguous (an
    unadjudicated tie is never quietly resolved); else any missing REQUIRED operand marks it
    missing (an absent optional operand degrades, it does not block); else bound."""
    required = {op.role for op in definition.operands if op.required}
    by_status: dict[str, set[str]] = {}
    for verdict in verdicts:
        by_status.setdefault(verdict.status, set()).add(verdict.role)
    if by_status.get("blocked", set()) & required:
        return "blocked"
    if by_status.get("ambiguous", set()) & required:
        return "ambiguous"
    if by_status.get("unresolved", set()) & required:
        return "missing"
    return "bound"


@dataclass(frozen=True, slots=True)
class V2RecipeCandidateV1:
    """One eligible recipe, fully assembled for Gate-1: definition identity, planning request,
    shared-binder verdicts, binding state, compiled temporal (or its blocker), authored
    readiness, and revision-specific review validity. Everything a candidate card or an option
    mint needs — computed once, carried whole."""

    recipe_id: str
    relationship: str                    # "primary" | "supporting"
    planning_request: FeaturePlanningRequestV1
    planning_request_hash: str
    recipe_revision_hash: str
    verdicts: tuple[OperandBindingVerdictV1, ...]
    binding_state: str                   # BINDING_STATES
    readiness: str                       # the authored RECIPE_READINESS value
    temporal_pit_text: str = ""          # compiled PIT clause text; "" when blocked
    temporal_blocker: str = ""           # the named reason compilation refused; "" when compiled
    review_current: bool = False
    review_missing_roles: tuple[str, ...] = ()
    # The full per-candidate eligibility audit from the shared binder — the losing-shortlist
    # evidence SE-10 persists: {(role, object_ref): OperandEligibilityVerdictV1}.
    eligibility: dict = None  # type: ignore[assignment]


def _review_validity(conn, definition: RecipeDefinitionV2,
                     revision_hash: str) -> tuple[bool, tuple[str, ...]]:
    from featuregen.overlay.upload.recipe_review_validity import (
        review_validity,
        reviews_by_role_for_revision,
    )

    by_role = reviews_by_role_for_revision(
        conn, recipe_id=definition.recipe_id, recipe_revision_hash=revision_hash)
    validity = review_validity(definition, by_role)
    return validity.current, validity.missing_roles


def _compile_temporal(definition: RecipeDefinitionV2) -> tuple[str, str]:
    """The compiler is already honestly two-state (compiled | blocked, blockers NAMED) —
    project that, never wrap it."""
    from featuregen.overlay.upload.recipe_temporal_v2 import compile_temporal

    compiled = compile_temporal(definition)
    if compiled.status == "compiled":
        return compiled.pit_text, ""
    return "", "; ".join(compiled.blockers) or "temporal compilation blocked"


def v2_recipe_candidates(conn, *, catalog_source: str, roles=(),
                         scope: ConfirmedScope, context=None,
                         ) -> tuple[V2RecipeCandidateV1, ...]:
    """Assemble every eligible recipe's candidate data for one catalog, primary first then
    authored registry order — deterministic, no score. Binding runs through the SHARED
    capability-input engine (`bind_planning_request`) over ONE frozen context — this lens and
    the formula/suggestion paths cannot disagree about a binding, and `graph_node` is read
    once per run, never per recipe."""
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash

    if context is None:
        context = build_generation_semantic_context(
            conn, catalog_source=catalog_source, roles=roles)
    applicability = v2_applicability(scope)
    ordered = [recipe for recipe in V2_RECIPES
               if applicability.by_recipe[recipe.recipe_id] == "primary"]
    ordered += [recipe for recipe in V2_RECIPES
                if applicability.by_recipe[recipe.recipe_id] == "supporting"]

    candidates: list[V2RecipeCandidateV1] = []
    for recipe in ordered:
        request = planning_request_from_recipe(recipe)
        verdicts, eligibility = bind_planning_request(conn, request, context)
        revision_hash = canonical_recipe_v2_hash(recipe)
        pit_text, temporal_blocker = _compile_temporal(recipe)
        current, missing_roles = _review_validity(conn, recipe, revision_hash)
        candidates.append(V2RecipeCandidateV1(
            recipe_id=recipe.recipe_id,
            relationship=applicability.by_recipe[recipe.recipe_id],
            planning_request=request,
            planning_request_hash=planning_request_hash(request),
            recipe_revision_hash=revision_hash,
            verdicts=verdicts,
            binding_state=fold_binding_state(verdicts, recipe),
            readiness=recipe.readiness,
            temporal_pit_text=pit_text,
            temporal_blocker=temporal_blocker,
            review_current=current,
            review_missing_roles=missing_roles,
            eligibility=eligibility))
    return tuple(candidates)


__all__ = [
    "BINDING_STATES", "V2ApplicabilityResult", "V2RecipeCandidateV1",
    "fold_binding_state", "v2_applicability", "v2_recipe_candidates",
]
