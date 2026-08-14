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
  temporal contract (or its named blocker), BR-7's FOLDED readiness with its named blockers
  (task A6 — never the definition's authored literal, which survives only as an assertion the
  fold must not contradict at registry-validation time), and the BR-23 review validity AT the
  recipe's current revision.

What this module deliberately does NOT do: project candidates into `FeatureIdea`, mint options,
or touch the considered set — that is `semantic_projection` and the gate1 wiring. Since the E4
cutover (2026-08-14) this lens is not selected by anything: it is the ONLY recipe lens the
hypothesis path has, called unconditionally.

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
)
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope, ScopeExpansion
from featuregen.overlay.upload.taxonomy.use_cases import descendants

#: The candidate-level fold of per-operand verdicts (plan §6.7's semantic-binding axis).
BINDING_STATES = ("bound", "ambiguous", "missing", "blocked")

#: A6's fail-closed fallback: a REQUIRED operand that did not bind but whose verdict attached no
#: BR-5 reason code. Unreachable on today's binder (every non-bound required path names a code),
#: and kept because the dangerous direction is the silent one — a dropped blocker PROMOTES a
#: candidate up the readiness ladder, and a fold must never do that because a verdict forgot to
#: explain itself.
BLOCKER_OPERAND_NOT_BOUND = "operand_not_bound"


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


@dataclass(frozen=True, slots=True)
class DatasetStoryV1:
    """SE-8 steps 2+3 — the candidate's FEATURE-LEVEL dataset decision, folded once over every
    bound operand (never one column at a time):

    * ``population_ref`` — the table whose rows define WHO this feature computes over. It is
      EXPLICIT or it is absent: the population is the table of the bound entity-key operand
      whose grain flag was DECLARED in the upload (a governed fact) — never inferred from a
      table's name or its primary entity (step 3's rule, verbatim).
    * ``dataset_tables`` — every table a bound operand touches; ``cross_dataset`` says the
      candidate spans more than one, which makes the RELATIONSHIP a feature-level need.
    * ``codes`` — the named setup work: POPULATION_DATASET_UNDECLARED when no declared-grain
      entity key anchors the population; RELATIONSHIP_REQUIRED when the candidate crosses
      datasets (until relationship facts prove the hop, the honest state is setup work)."""

    population_ref: str | None
    population_basis: str                # "declared_grain" | "undeclared"
    dataset_tables: tuple[str, ...]
    cross_dataset: bool
    codes: tuple[str, ...]


def fold_dataset_story(request, verdicts, context) -> DatasetStoryV1:
    """The pure fold: bound refs + the frozen context's declared facts, nothing else."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R

    by_ref = {c.object_ref: c for c in context.columns} if context is not None else {}
    operands_by_role = {op.role: op for op in request.operands}
    tables: list[str] = []
    population: str | None = None
    for verdict in verdicts:
        if verdict.status != "bound" or not verdict.selected_ref:
            continue
        column = by_ref.get(verdict.selected_ref)
        if column is None:
            continue
        if column.table not in tables:
            tables.append(column.table)
        operand = operands_by_role.get(verdict.role)
        if (operand is not None and operand.operand_class == "entity_key"
                and column.is_grain and population is None):
            population = column.table
    codes: list[str] = []
    if population is None:
        codes.append(R.POPULATION_DATASET_UNDECLARED)
    if len(tables) > 1:
        codes.append(R.RELATIONSHIP_REQUIRED)
    return DatasetStoryV1(
        population_ref=population,
        population_basis="declared_grain" if population else "undeclared",
        dataset_tables=tuple(tables),
        cross_dataset=len(tables) > 1,
        codes=tuple(codes))


def v2_applicability_as_result(scope: ConfirmedScope):
    """The V2 classification in the LEGACY ``ApplicabilityResult`` carrier — so the disposition
    lens folds the universe that was actually planned (the V2 registry), not the legacy one,
    without growing a second disposition path. Reason codes use
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


_WINDOW_TOKEN = None  # compiled lazily


def _hypothesis_window_tokens(redacted_hypothesis: str) -> set:
    """Deterministic token pass (B5 — no LLM): the integer day-counts a hypothesis names.
    "90-day", "90 day", "90d" → {90}; "3 months" → {90} (banking-calendar 30-day months)."""
    import re
    global _WINDOW_TOKEN
    if _WINDOW_TOKEN is None:
        _WINDOW_TOKEN = re.compile(
            r"(\d+)\s*-?\s*(day|days|d\b|week|weeks|month|months)", re.IGNORECASE)
    tokens: set = set()
    for number, unit in _WINDOW_TOKEN.findall(redacted_hypothesis or ""):
        factor = {"week": 7, "weeks": 7, "month": 30, "months": 30}.get(unit.lower(), 1)
        tokens.add(int(number) * factor)
    return tokens


def _variant_axes(recipe):
    """The enumerable parameters: everything except governed_policy references (those are
    reviewed policies, never free literals to sweep)."""
    return [p for p in recipe.parameters
            if p.parameter_class != "governed_policy" and p.allowed_values]


def _enumerate_variant_requests(ordered, redacted_hypothesis: str):
    """B5 (GEN-03 closed): every authored parameterization is its OWN planning request —
    bounded by the registry's authoring (≈940 total). The PRIMARY variant per recipe is the
    deterministic hypothesis match (a "90 day" hypothesis leads with the 90-day variant) or
    the authored-first values when nothing matches. Binding cost is folds, not queries (B6)."""
    from itertools import product

    tokens = _hypothesis_window_tokens(redacted_hypothesis)
    out = []
    for recipe in ordered:
        axes = _variant_axes(recipe)
        if not axes:
            out.append((recipe, planning_request_from_recipe(recipe), {}, True))
            continue
        combos = [dict(zip((p.name for p in axes), values))
                  for values in product(*(p.allowed_values for p in axes))]
        primary = combos[0]                                   # authored-first default
        for combo in combos:
            if any(isinstance(v, int) and v in tokens for v in combo.values()):
                primary = combo
                break
        for combo in combos:
            out.append((recipe, planning_request_from_recipe(recipe, parameter_values=combo),
                        combo, combo == primary))
    return out


def fold_frozen_binding_plan(request, verdicts, story, pit_text: str,
                             temporal_blocker: str, catalog_source: str,
                             uoa_entity: str | None = None):
    """B7 (PLAN-01 closed for the single-source class) — the plan is a VALIDATION over the
    exact frozen bindings, never a second binder:

    * the read set IS the bound refs (divergence impossible by construction; the defensive
      check below still runs — a bound ref outside the story's tables is
      BINDING_PLAN_DIVERGENCE, never a silent substitution);
    * single-source + declared population + compiled temporal → a real plan
      {source, population, read set, PIT, grain, window};
    * cross-dataset (until governed join facts prove the hop) or an uncompiled temporal
      contract → NO plan, with the named refusal — actionable, never governable.

    The governed CROSS-catalog envelope (physical_plan_id + catalog fingerprints) remains the
    3C.2a planner's job; this fold is the single-source half the semantic path serves today.
    Returns (plan | None, refusal_codes)."""
    from featuregen.overlay.upload import semantic_eligibility_reasons as R

    bound = {v.role: v.selected_ref for v in verdicts
             if v.status == "bound" and v.selected_ref}
    if not bound:
        return None, (R.BINDING_NOT_BOUND,)
    if story is None or story.cross_dataset:
        return None, (R.RELATIONSHIP_REQUIRED,)
    if temporal_blocker:
        return None, (R.TEMPORAL_POLICY_UNRESOLVED,)
    if story.population_ref is None:
        return None, (R.POPULATION_DATASET_UNDECLARED,)
    # B10: the plan computes AT the confirmed unit of analysis or not at all — a candidate
    # whose output grain differs is honest setup work ("roll it up via a matching-grain
    # recipe, or change the confirmed UOA"), never a silently-served mismatch.
    # Case-insensitive: the UOA is human vocabulary — the catalog declares "Customer", the
    # planning grain says "customer"; a capitalization is never a different unit of analysis.
    if (uoa_entity and request.output_grain
            and request.output_grain.strip().casefold() != uoa_entity.strip().casefold()):
        return None, (R.UOA_MISMATCH,)
    # Defensive divergence check: every bound ref's table must be one the dataset story saw.
    read_set = tuple(sorted(bound.values()))
    tables = set(story.dataset_tables)
    for ref in read_set:
        table = ref.split(".")[-2] if ref.count(".") >= 2 else ""
        if table and table not in tables:
            return None, (R.BINDING_PLAN_DIVERGENCE,)
    window = dict(request.parameter_values).get("window")
    return {
        "plan_kind": "single_source",
        "catalog_source": catalog_source,
        "source_table": story.dataset_tables[0] if story.dataset_tables else "",
        "population_ref": story.population_ref,
        "read_set": list(read_set),
        "role_bindings": {role: ref for role, ref in sorted(bound.items())},
        "pit": pit_text,
        "output_grain": request.output_grain,
        "window": window,
    }, ()


def _variant_key(recipe_id: str, variant: dict) -> str:
    if not variant:
        return recipe_id
    slug = ";".join(f"{k}={variant[k]}" for k in sorted(variant))
    return f"{recipe_id}@{slug}"


def _param_alternatives_text(recipe, variant: dict) -> str:
    """The card's honest 'also available' line: every axis with the CHOSEN value bracketed."""
    parts = []
    for p in _variant_axes(recipe):
        chosen = variant.get(p.name)
        rendered = "/".join(f"[{v}]" if v == chosen else str(v) for v in p.allowed_values)
        parts.append(f"{p.name}: {rendered}")
    return "; ".join(parts)


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


def binding_blockers(verdicts: tuple[OperandBindingVerdictV1, ...],
                     definition) -> tuple[str, ...]:
    """A6 — BR-5's own reason codes for the REQUIRED operands that did not bind, verbatim.

    The severity law is :func:`fold_binding_state`'s, restated: an absent OPTIONAL operand
    degrades the feature, it never blocks it, so only required roles contribute. Codes are
    deduplicated in verdict order (the recipe's authored operand order), so the blocker list is
    as deterministic as the candidate itself.
    """
    required = {op.role for op in definition.operands if op.required}
    codes: list[str] = []
    for verdict in verdicts:
        if verdict.status == "bound" or verdict.role not in required:
            continue
        codes.extend(verdict.reason_codes or (BLOCKER_OPERAND_NOT_BOUND,))
    return tuple(dict.fromkeys(codes))


@dataclass(frozen=True, slots=True)
class V2RecipeCandidateV1:
    """One eligible recipe, fully assembled for Gate-1: definition identity, planning request,
    shared-binder verdicts, binding state, compiled temporal (or its blocker), FOLDED readiness,
    and revision-specific review validity. Everything a candidate card or an option mint needs —
    computed once, carried whole."""

    recipe_id: str
    relationship: str                    # "primary" | "supporting"
    planning_request: FeaturePlanningRequestV1
    planning_request_hash: str
    recipe_revision_hash: str
    verdicts: tuple[OperandBindingVerdictV1, ...]
    binding_state: str                   # BINDING_STATES
    # A6: BR-7's FOLD over this candidate's own measurements (the definition's declarations, its
    # compiled temporal, its binding verdicts), never the definition's authored literal. The
    # literal survives on `RecipeDefinitionV2` as an assertion the fold must not contradict,
    # checked at registry-validation time.
    readiness: str                       # READINESS_LADDER — folded
    # The fold's own named blockers, carried beside the state so "why is it not ready?" is a
    # list of facts some deterministic gate produced, not a shrug.
    readiness_blockers: tuple[str, ...] = ()
    temporal_pit_text: str = ""          # compiled PIT clause text; "" when blocked
    temporal_blocker: str = ""           # the named reason compilation refused; "" when compiled
    review_current: bool = False
    review_missing_roles: tuple[str, ...] = ()
    # The full per-candidate eligibility audit from the shared binder — the losing-shortlist
    # evidence SE-10 persists: {(role, object_ref): OperandEligibilityVerdictV1}.
    eligibility: dict = None  # type: ignore[assignment]
    # SE-8 steps 2+3: the feature-level dataset decision (population + cross-dataset need),
    # folded from the frozen context's DECLARED facts. None only on legacy fixtures.
    dataset_story: DatasetStoryV1 | None = None
    # B7: the FROZEN-BINDINGS plan — source/read-set/PIT/grain computed OVER the exact bound
    # refs (built FROM the verdicts, so the planner can never choose different columns than
    # the user saw). None = no plan exists yet (cross-dataset without a verified path,
    # temporal blocked, or nothing bound) — the activation fold keeps create_contract blocked.
    binding_plan: dict | None = None
    plan_refusals: tuple = ()
    # B5: the variant identity — recipe_id + this candidate's exact parameter choice. Every
    # authored parameterization is its OWN candidate; the variant key is what capture, facts,
    # and option identity key on (recipe_id stays the DISPOSITION/review key).
    variant_key: str = ""
    variant_primary: bool = True
    param_alternatives: str = ""
    # B4: the candidate's own business definition for the card — recipes carry the authored
    # business_definition, intents carry the model's. DISPLAY ONLY: deliberately not on the
    # planning request, whose hash is field-exhaustive (prose must never be identity).
    display_definition: str = ""


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
                         redacted_hypothesis: str = "",
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

    # B6 (PLAN-13 closed): the run's DB work is O(fact families), never O(recipes) —
    # ONE batched capability read over the union of every recipe's shortlists, ONE review-event
    # read for the whole store, then pure folds per recipe.
    from featuregen.overlay.upload.column_capabilities import compile_capabilities
    from featuregen.overlay.upload.recipe_operand_policy import (
        bind_with_capabilities,
        request_shortlists,
    )
    from featuregen.overlay.upload.recipe_review import review_events_all
    from featuregen.overlay.upload.recipe_review_validity import (
        by_role_at_revision,
        review_validity,
    )

    requests = _enumerate_variant_requests(ordered, redacted_hypothesis)
    union_refs = list(dict.fromkeys(
        ref for _recipe, request, _variant, _primary in requests
        for refs in request_shortlists(request, context).values() for ref in refs))
    capabilities = compile_capabilities(conn, context, union_refs)
    events_by_recipe = review_events_all(conn)

    candidates: list[V2RecipeCandidateV1] = []
    from featuregen.overlay.upload.recipe_readiness import fold_definition_readiness

    for recipe, request, variant, is_primary in requests:
        verdicts, eligibility = bind_with_capabilities(conn, request, context, capabilities)
        revision_hash = canonical_recipe_v2_hash(recipe)
        pit_text, temporal_blocker = _compile_temporal(recipe)
        validity = review_validity(
            recipe, by_role_at_revision(events_by_recipe.get(recipe.recipe_id, []),
                                        revision_hash))
        current, missing_roles = validity.current, validity.missing_roles
        # A6: BR-7's fold, on the serving path at last — over the definition's declarations,
        # this variant's compiled temporal, and the binder's verdicts for THIS catalog. The
        # authored literal is no longer served: a recipe whose operands did not bind here says
        # FORMULA_BLOCKED with the reasons, not FORMULA_AUTHORABLE because a registry said so.
        folded = fold_definition_readiness(
            recipe,
            temporal_blockers=(temporal_blocker,) if temporal_blocker else (),
            binding_blockers=binding_blockers(verdicts, recipe))
        candidates.append(V2RecipeCandidateV1(
            recipe_id=recipe.recipe_id,
            relationship=applicability.by_recipe[recipe.recipe_id],
            planning_request=request,
            planning_request_hash=planning_request_hash(request),
            recipe_revision_hash=revision_hash,
            verdicts=verdicts,
            binding_state=fold_binding_state(verdicts, recipe),
            readiness=folded.state,
            readiness_blockers=folded.blockers,
            temporal_pit_text=pit_text,
            temporal_blocker=temporal_blocker,
            review_current=current,
            review_missing_roles=missing_roles,
            eligibility=eligibility,
            dataset_story=(story := fold_dataset_story(request, verdicts, context)),
            binding_plan=(plan_and_refusals := fold_frozen_binding_plan(
                request, verdicts, story, pit_text, temporal_blocker,
                catalog_source, uoa_entity=getattr(scope, "uoa_entity", None)))[0],
            plan_refusals=plan_and_refusals[1],
            display_definition=recipe.business_definition,
            variant_key=_variant_key(recipe.recipe_id, variant),
            variant_primary=is_primary,
            param_alternatives=_param_alternatives_text(recipe, variant)))
    return tuple(candidates)


def llm_intent_candidates(conn, client, *, context, scope_leaves,
                          redacted_hypothesis: str, actor=None,
                          confirmed_scope_hash: str = "",
                          uoa_entity: str | None = None):
    """SE-6 wire-up — LLM intents through the SAME engine as recipes: one audited structured
    call proposes ABSTRACT intents (concepts, operand classes, temporal contracts — never
    physical refs), each adapts to the neutral planning request, and the SHARED capability
    binder decides WHICH columns serve. Returns (candidates, rejections): candidates in the
    same V2RecipeCandidateV1 carrier the recipe lens emits, so assembly merges recipe/LLM
    twins by semantic signature and the projection serves both origin-blind. Deterministic
    intents project as conceptual_pattern ("formula pending") — the structural readiness
    ceiling; no temporal PIT text is claimed (nothing was compiled from an authored recipe)."""
    from featuregen.overlay.upload.feature_intent_generation import generate_feature_intents
    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_feature_intent,
        planning_request_hash,
    )

    result = generate_feature_intents(
        conn, client, context=context, scope_leaves=scope_leaves,
        redacted_hypothesis=redacted_hypothesis, actor=actor,
        confirmed_scope_hash=confirmed_scope_hash)
    candidates = []
    rejections = [dict(r) for r in result.rejections]
    # B6: one batched capability read across ALL intents, pure folds per intent.
    from featuregen.overlay.upload.column_capabilities import compile_capabilities
    from featuregen.overlay.upload.recipe_operand_policy import (
        bind_with_capabilities,
        request_shortlists,
    )

    intent_requests = [(intent, planning_request_from_feature_intent(intent))
                       for intent in result.intents]
    union_refs = list(dict.fromkeys(
        ref for _intent, request in intent_requests
        for refs in request_shortlists(request, context).values() for ref in refs))
    capabilities = compile_capabilities(conn, context, union_refs)
    for intent, request in intent_requests:
        verdicts, eligibility = bind_with_capabilities(conn, request, context, capabilities)
        candidates.append(V2RecipeCandidateV1(
            recipe_id=request.source_definition_id,
            relationship="primary",
            planning_request=request,
            planning_request_hash=planning_request_hash(request),
            recipe_revision_hash=request.source_content_hash,
            verdicts=verdicts,
            binding_state=fold_binding_state(verdicts, request),
            readiness="CONCEPTUAL_ONLY",
            temporal_pit_text="",
            temporal_blocker="",
            review_current=False,
            review_missing_roles=(),
            eligibility=eligibility,
            dataset_story=(istory := fold_dataset_story(request, verdicts, context)),
            binding_plan=(iplan := fold_frozen_binding_plan(
                request, verdicts, istory, "", "", context.catalog_source,
                uoa_entity=uoa_entity))[0],
            plan_refusals=iplan[1],
            display_definition=intent.business_definition,
            variant_key=request.source_definition_id))
    return tuple(candidates), rejections


__all__ = [
    "BINDING_STATES", "BLOCKER_OPERAND_NOT_BOUND", "DatasetStoryV1", "V2ApplicabilityResult",
    "V2RecipeCandidateV1", "binding_blockers", "fold_binding_state", "fold_dataset_story",
    "llm_intent_candidates", "v2_applicability", "v2_applicability_as_result",
    "v2_recipe_candidates",
]
