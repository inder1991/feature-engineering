"""S1A-4b — the complete governed option builder: planning REQUESTS in, governed options out.

``gate1._governed_cross_catalog_options`` builds cross-catalog options from legacy ``Template``
objects and returns bare :class:`FeatureIdea` s. This module is the origin-neutral successor: it
takes :class:`FeaturePlanningRequestV1` s (a V2 recipe, an LLM intent, a user definition — one
shape), plans each through the ONE planner entry, and returns a
:class:`GovernedOptionV1` carrying the idea TOGETHER with the three things a consumer previously
had to re-derive (and could re-derive differently): the variant IDENTITY, the origin's
GOVERNANCE state, and the READINESS the fold produced. Display text rides alongside, never inside
identity.

Two properties are structural rather than conventional:

* **Origin purity.** A ``recipe_v2`` request is the ONLY origin that reaches the V2 registry, the
  review store or the expectation registry. An ``llm_intent`` request gets the neutral governance
  state with ZERO lookups, and its id never wears a recipe badge (``FeatureIdea.recipe_id`` stays
  ``None``; ``source_definition_id`` carries the origin-neutral id).
* **One batched read per evidence kind.** Enrichment runs in TWO passes: plan everything, then
  read the resolution pins, the governed key entities and the review events ONCE each over the
  union of the resolved plans, and project every option from that frozen batch. No option re-reads
  anything, so the read count does not grow with the option count.

WHAT THIS BUILDER CANNOT DO TODAY (measured on the way in, 2026-08-23) — three planner gaps stand
between a V2 RECIPE and a resolved governed cross-catalog contract. None of them is in this
module, and this module may not fix them (S1A-4b owns no planner file):

* **G1 — no binding roles on a request-contract probe.** ``planning_probe`` projects each operand's
  ``join_role``/``temporal_role`` verbatim, and ZERO of the 1195 operands in the 317-recipe V2
  registry declares either (``metadata_resolution_mode="request_contract"`` deliberately does not
  consult the legacy resolved-need registry, which is where a legacy Template's roles are DERIVED).
  ``plan._assemble_rollups`` starts a roll-up only from a binding whose ``join_role`` is
  ``source_entity_key``, so a recipe-origin request assembles no cross-catalog plan at all.
* **G3 — a governed BRIDGE hop carries no realization revision.** ``plan_bindings`` never runs
  ``assembly.attach_executable_bridge_realizations``, and ``build_compiler_context`` correctly
  leaves ``allow_provisional_bridge_cardinality`` false (it is sandbox-only), so a bridge hop's
  physical cardinality is UNAVAILABLE and any measure staged there fails
  ``physical_cardinality_unavailable``.
* **G2 — every non-key, non-time operand derives to a MEASURE.** ``need_metadata._derive_one``
  (the derivation G1's fix would apply) maps an operand with no ``entity_link`` and no ``pit_role``
  — a ``status``, a ``dimension``, a ``direction`` — to ``JoinRole.MEASURE``, so the contract
  fails for an operand nobody intended to aggregate. The V2 ``operand_class`` vocabulary is never
  projected into ``JoinRole``.

**The order matters, and an earlier revision of this docstring got it wrong.** The three gaps are
SEQUENTIAL, not parallel, and only one code is ever emitted at a time:

1. G1 blocks DISCOVERY: no roll-up is assembled, so no contract compiles and no code is emitted;
2. once G1 closes, a measure crossing the governed bridge hits G3 FIRST —
   ``compile_aggregation`` short-circuits on ``card is None`` (``declarations.py``) and records
   ``physical_cardinality_unavailable`` WITHOUT ever running the additivity matrix. That is the
   primary refusal a cross-catalog recipe actually gets, and
   ``test_the_measured_refusal_sequence_is_g3_before_g2`` pins it against the real planner;
3. G2's ``aggregation_axis_unsupported`` is DOWNSTREAM of G3 — it is masked behind that
   short-circuit and surfaces only once a cardinality is available, which today happens on an
   INTRA-catalog realization hop (whose declared join supplies one) and will happen on a bridge
   hop once G3 closes. The same test pins that half too.

The consequence for consumers: a request whose operands DECLARE their binding roles (an LLM
intent may; the platform's own ``derive_need_metadata`` resolves them for any probe) reaches a
resolved governed cross-catalog contract through this builder today. A request built by
``planning_request_from_recipe`` alone does not — it becomes an evidence-bearing REJECTION, which
is the honest answer until the planner seam closes.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from featuregen.overlay.upload.catalog_realizations import key_entities_for, table_of
from featuregen.overlay.upload.contract.governed_identity import (
    DEFINITION_ORIGIN_RECIPE_V2,
    GovernedVariantIdentityV1,
)
from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement, RoleBinding
from featuregen.overlay.upload.feature_metadata_snapshot import capture_column_snapshot
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.field_resolution import ResolutionPinV1, current_resolution_pins
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.planner.contracts import (
    BindingPlanningResultV1,
    BindingPlanV1,
    ContractResolutionStatus,
    PathResolutionStatus,
    ReasonCode,
    full_physical_plan_hash,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget, build_compiler_context
from featuregen.overlay.upload.planner.fingerprint import contract_input_hash
from featuregen.overlay.upload.planner.plan_envelope import (
    PlanEnvelopeV1,
    plan_envelope_from_result,
)
from featuregen.overlay.upload.planner.requests import plan_planning_request, planning_probe
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.planner.shadow import COMPILE_BUDGET, MAX_COMPILES_PER_RUN
from featuregen.overlay.upload.recipe_formula_expectations_v2 import has_reviewed_expectation

# `_scalar` is the parameter-value coercion the SAME producer applies before hashing
# (`semantic_parameters`). Imported for validation parity: hashing a value that producer would
# refuse is how two spellings of one parameter binding get two hashes.
from featuregen.overlay.upload.recipe_grounding_context import (
    _scalar as _scalar_parameter_value,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    canonical_recipe_v2_hash,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.recipe_readiness import RecipeReadinessV1, fold_request_readiness
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.recipe_review import review_events_all

# `_policy_refs` is the ONE pooling of a recipe's governed policy references (eligibility +
# operand status policies + parameter policies). Imported rather than re-implemented on purpose:
# a second pooling could drift from the one the review-role law reads, and then "which policies
# govern this recipe?" would have two answers. (Worth promoting to a public name.)
from featuregen.overlay.upload.recipe_review_validity import (
    _policy_refs as _pooled_policy_refs,
)
from featuregen.overlay.upload.recipe_review_validity import (
    by_role_at_revision,
    review_validity,
)

# NOTE: `validation_requirements.build_requirement` — the ONLY sanctioned Requirement factory — is
# deliberately NOT imported: every builder in `_REQUIREMENT_BUILDERS` currently refuses, so nothing
# here mints one. Re-import it in the task that adds the first real builder (see the mapping's
# key-set comment for which code that is and what context it needs).

logger = logging.getLogger(__name__)

_MAX_RATIONALE = 200
_REPEATABLE_READ = "repeatable read"
_LENS = "governed"


# ── the carriers ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DefinitionGovernanceStateV1:
    """The governance answers about a definition, ORIGIN-NEUTRAL in shape.

    A recipe origin fills these from the registry, the review store and the expectation registry.
    Every other origin gets the neutral state — all-False, all-empty — which says "nothing governs
    this yet", never "this is approved". No field here can be asserted by a caller."""

    retired: bool
    review_current: bool
    review_missing_roles: tuple[str, ...]
    reviewed_expectation: bool
    policy_revision_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GovernedOptionV1:
    """ONE governed cross-catalog option: the idea, its identity, its governance, its readiness.

    ``readiness`` is the FOLD's output — never authored text, and never a claim this module made.
    ``display_name`` / ``business_definition`` are display, and display is never identity: two
    options with the same ``identity`` and different labels are the same option relabelled."""

    idea: FeatureIdea
    request: FeaturePlanningRequestV1
    identity: GovernedVariantIdentityV1
    governance: DefinitionGovernanceStateV1
    readiness: RecipeReadinessV1
    display_name: str
    business_definition: str
    # `FeatureIdea` has no field for these: a reason code with no closed requirement builder is a
    # fact about THIS option's evidence, carried here so it is visible instead of discarded.
    unmapped_requirement_codes: tuple[str, ...]


_NEUTRAL_GOVERNANCE = DefinitionGovernanceStateV1(
    retired=False, review_current=False, review_missing_roles=(),
    reviewed_expectation=False, policy_revision_ids=())


# ── requirements: CLOSED builders, never a string map ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReasonContextV1:
    """One observed reason code and the operand it concerns — a requirement's whole input."""

    code: ReasonCode
    role: str
    catalog_source: str
    object_ref: str


def _stays_a_refusal(context: ReasonContextV1) -> Requirement | None:
    """A HARD blocker: no external data check can clear it, so it never becomes a requirement.

    A structural refusal (an unsafe binding, a leakage or protected-attribute read, a
    non-aggregating measure on a fan-in hop) and a GOVERNANCE gap (a missing or conflicting
    aggregation declaration, an absent or ambiguous temporal anchor) are both answered by a
    person changing a declaration — never by measuring the data. Mapping either to a requirement
    would tell a reviewer to go run a check that cannot change the outcome."""
    del context
    return None


#: The mapping's key set, DECLARED so an addition is deliberate (pinned by test). Every key is a
#: real ``ReasonCode``, and TODAY every one of them maps to ``_stays_a_refusal``: no code this
#: build can emit has both a closed requirement code whose semantic matches exactly AND an operand
#: this module can name correctly. Three codes are deliberately ABSENT, each for its own reason —
#: all three land in ``unmapped_requirement_codes``, the honest carrier, where they stay visible:
#:
#: * ``additivity_source_conflict`` — its natural target ``ADDITIVITY_SUPPORTS_OPERATION`` REQUIRES
#:   an ``operation`` parameter that :class:`ReasonContextV1` does not carry. Minting it would need
#:   a schema-invalid requirement, the production defect this closed-builder design exists to stop.
#: * ``ingredient_not_connected_to_path`` — a STRUCTURAL refusal: it folds to
#:   ``unresolved_ingredient_connectivity``, which no data check can clear, so by the
#:   ``_stays_a_refusal`` doctrine it must not become a requirement. It also names no operand (the
#:   compiler records the code, not the disconnected role), so a ``JOIN_CONNECTIVITY`` requirement
#:   built here would name a column it is not about.
#: * ``physical_cardinality_unavailable`` — a ``GRAIN_IS_UNIQUE`` check IS the right external
#:   answer, but the right OPERAND is the FAILING HOP's destination key, and a plan carries only
#:   its final ``output_grain_ref``. On any multi-hop path those differ, so the requirement would
#:   name the wrong key. This is the one code that becomes live when G3 closes: the task that
#:   closes it should design this requirement WITH hop context (``HopAggregationV1`` already
#:   carries ``grouping_keys`` and the execution site), and re-import
#:   ``validation_requirements.build_requirement``, which is the sanctioned factory and is
#:   deliberately not imported here while nothing mints.
REQUIREMENT_BUILDER_CODES: tuple[ReasonCode, ...] = (
    ReasonCode.aggregation_axis_unsupported,
    ReasonCode.aggregation_strategy_missing,
    ReasonCode.aggregation_declaration_conflict,
    ReasonCode.semi_additive_temporal_strategy_missing,
    ReasonCode.temporal_anchor_missing,
    ReasonCode.temporal_anchor_ambiguous,
    ReasonCode.leakage_anchor_read,
    ReasonCode.protected_attribute_read,
    ReasonCode.binding_safety_rejected,
)

_REQUIREMENT_BUILDERS: dict[ReasonCode, Callable[[ReasonContextV1], Requirement | None]] = {
    code: _stays_a_refusal for code in REQUIREMENT_BUILDER_CODES
}

#: Codes that describe the PATH, not one ingredient — and that no ref this module holds names
#: correctly. They are carried WITHOUT an operand: the plan's final ``output_grain_ref`` is NOT
#: the failing hop's destination key on a multi-hop path, and the staged measure that surfaced the
#: code is not its subject either. A ref-less context can never mint a requirement (every builder
#: needs an operand), so the wrong-anchor hazard is closed structurally rather than by convention.
_PATH_LEVEL_CODES = frozenset({
    ReasonCode.physical_cardinality_unavailable,
    ReasonCode.ingredient_not_connected_to_path,
})


def _reason_contexts(plan: BindingPlanV1) -> tuple[ReasonContextV1, ...]:
    """Every reason code this plan OBSERVED, paired with the operand it concerns — or with NO
    operand where naming one would be a guess.

    Per-ingredient codes come from the binding (candidate stage) and from the aggregation stage
    (contract stage), both of which name their own operand. A path-level code
    (:data:`_PATH_LEVEL_CODES`) and any leftover contract code are carried ref-less: no operand
    this plan holds is their subject, and a ref-less context can never mint a requirement about a
    column it does not concern."""
    catalog_of = {b.need_role: b.bound_catalog_source for b in plan.ingredient_bindings}
    contexts: list[ReasonContextV1] = []
    placed: set[ReasonCode] = set()
    for binding in plan.ingredient_bindings:
        for code in binding.reason_codes:
            contexts.append(ReasonContextV1(
                code=code, role=binding.need_role, catalog_source=binding.bound_catalog_source,
                object_ref=binding.bound_object_ref))
            placed.add(code)
    for hop in plan.hop_aggregations:
        for stage in hop.ingredient_stages:
            for code in stage.reason_codes:
                if code in _PATH_LEVEL_CODES:
                    continue        # ref-less below — the staged measure is not its subject
                contexts.append(ReasonContextV1(
                    code=code, role=stage.need_role,
                    catalog_source=catalog_of.get(stage.need_role, ""),
                    object_ref=stage.bound_object_ref))
                placed.add(code)
    for code in plan.contract_reason_codes:
        if code in placed:
            continue
        contexts.append(ReasonContextV1(code=code, role="", catalog_source="", object_ref=""))
        placed.add(code)
    return tuple(contexts)


def _project_requirements(
        contexts: Sequence[ReasonContextV1]) -> tuple[tuple[Requirement, ...], tuple[str, ...]]:
    """The closed projection: (requirements, unmapped codes).

    A code with a builder that returns ``None`` stays a REFUSAL — it is neither a requirement nor
    "unmapped": the mapping considered it and said no. A code with NO builder is unmapped, and
    says so out loud."""
    requirements: list[Requirement] = []
    unmapped: list[str] = []
    for context in contexts:
        builder = _REQUIREMENT_BUILDERS.get(context.code)
        if builder is None:
            unmapped.append(context.code.value)
            continue
        requirement = builder(context)
        if requirement is not None:
            requirements.append(requirement)
    return tuple(dict.fromkeys(requirements)), tuple(dict.fromkeys(unmapped))


# ── identity: ONE derivation, asserted at its boundary ────────────────────────────────────────


def _governed_variant_identity(request: FeaturePlanningRequestV1, *,
                               physical_plan_content_hash, parameter_binding_hash,
                               ) -> GovernedVariantIdentityV1:
    """Construct the variant identity, refusing every way it is quietly gettable WRONG.

    Called from exactly one place (:func:`_variant_identity_for`); separate from it so each
    refusal is reachable in a test without fabricating a whole compiled plan.

    * ``physical_plan_content_hash`` must be 64 lowercase hex. The truncated ``bp_…`` display id
      is the trap: it is a well-formed string that mints a well-formed but WRONG variant id.
    * ``parameter_binding_hash`` must be ``""`` or hex — never ``None`` (``str(None)`` would hash
      the literal ``"None"`` and silently collapse two parameter variants onto one id).
    * ``canonical_definition_id`` may not contain ``"|"``, the identity material's separator.
    """
    if (not isinstance(physical_plan_content_hash, str)
            or len(physical_plan_content_hash) != 64
            or any(c not in "0123456789abcdef" for c in physical_plan_content_hash)):
        raise ValueError(
            "physical_plan_content_hash must be the FULL 64-char lowercase hex digest, got "
            f"{physical_plan_content_hash!r} (a truncated bp_ display id mints a wrong variant id)")
    if (not isinstance(parameter_binding_hash, str)
            or (parameter_binding_hash
                and any(c not in "0123456789abcdef" for c in parameter_binding_hash))):
        raise ValueError(
            "parameter_binding_hash must be '' or a lowercase hex digest — never None and never "
            f"str()-coerced, got {parameter_binding_hash!r}")
    if "|" in request.source_definition_id:
        raise ValueError(
            "canonical_definition_id may not contain the identity separator '|', got "
            f"{request.source_definition_id!r}")
    return GovernedVariantIdentityV1(
        canonical_definition_id=request.source_definition_id,
        definition_origin=request.origin,
        planning_request_hash=planning_request_hash(request),
        physical_plan_content_hash=physical_plan_content_hash,
        parameter_binding_hash=parameter_binding_hash,
        plan_envelope_version="1")


def _parameter_binding_hash(request: FeaturePlanningRequestV1) -> str:
    """The SEMANTIC parameter binding hash the engine mints elsewhere
    (``recipe_grounding_context.semantic_parameter_hash``), reached from the request's own
    resolved ``(name, value)`` pairs. A request that binds no parameter has no parameter binding:
    ``""``, the honest absence the identity contract requires (never ``None``).

    Values pass through the sibling producer's own ``_scalar`` coercion, so an unsupported
    parameter type RAISES here exactly as it does in ``semantic_parameters`` — the same validation,
    rather than silently hashing a repr this side of the seam."""
    if not request.parameter_values:
        return ""
    return semantic_parameter_hash(
        request.source_definition_id,
        tuple((name, _scalar_parameter_value(value))
              for name, value in sorted(request.parameter_values, key=lambda pair: pair[0])))


def _variant_identity_for(request: FeaturePlanningRequestV1, plan: BindingPlanV1, *,
                          compile_ctx) -> GovernedVariantIdentityV1:
    """THE one place a governed variant identity is derived from a (request, plan) pair.

    ``physical_plan_content_hash`` is the compiled plan's ``contract_input_hash`` — the full
    64-hex digest of the inputs the verdict consumed — when the plan actually compiled, and the
    untruncated ``full_physical_plan_hash`` otherwise.

    NOTE (divergence from the S1A-4b brief, which anticipated ``contract_input_hash(plan)``):
    ``planner.fingerprint.contract_input_hash`` takes ``(ctx, plan, template)`` — the read columns
    it hashes live on the compiler context — so this helper takes the run's ``compile_ctx`` and
    projects the request's own probe as the template. Both are the run's, never re-derived."""
    compiled = (plan.path_resolution_status is PathResolutionStatus.source_to_target_resolved
                and plan.contract_resolution_status is not ContractResolutionStatus.not_compiled)
    content_hash = (contract_input_hash(compile_ctx, plan, planning_probe(request)) if compiled
                    else full_physical_plan_hash(plan))
    return _governed_variant_identity(
        request, physical_plan_content_hash=content_hash,
        parameter_binding_hash=_parameter_binding_hash(request))


# ── governance + display ──────────────────────────────────────────────────────────────────────


def _governance_for(request: FeaturePlanningRequestV1, *, events_by_recipe):
    """``(governance, definition)`` for this request's ORIGIN.

    A recipe origin is the only one that reads the registry, the review events (from the batch —
    never a per-recipe query) and the expectation registry. Every other origin returns the neutral
    state having read NOTHING: that is origin purity, and it is enforced by this early return."""
    if request.origin != DEFINITION_ORIGIN_RECIPE_V2:
        return _NEUTRAL_GOVERNANCE, None
    definition = v2_recipe_by_id(request.source_definition_id)
    if definition is None:
        # A recipe-origin request naming an id this build does not carry: the honest answer is
        # "nothing governs it here", never a fabricated approval.
        logger.warning("governed lens: recipe-origin request %s is not in the V2 registry",
                       request.source_definition_id)
        return _NEUTRAL_GOVERNANCE, None
    revision_hash = canonical_recipe_v2_hash(definition)
    validity = review_validity(
        definition,
        by_role_at_revision(events_by_recipe.get(definition.recipe_id, ()), revision_hash))
    formula = definition.formula
    return DefinitionGovernanceStateV1(
        retired=definition.readiness == "RETIRED",
        review_current=validity.current,
        review_missing_roles=tuple(validity.missing_roles),
        reviewed_expectation=(formula is not None
                              and has_reviewed_expectation(formula.expectation_ref)),
        policy_revision_ids=tuple(dict.fromkeys(_pooled_policy_refs(definition)))), definition


def _display_for(request: FeaturePlanningRequestV1, definition) -> tuple[str, str]:
    """``(display_name, business_definition)`` — the registry's words for a recipe the registry
    carries, the request's own for everything else. Display, never identity."""
    if definition is not None:
        return (definition.output.display_label or definition.recipe_id,
                definition.business_definition)
    return (request.output.display_label or request.primary_objective, request.primary_objective)


# ── the measured blockers the readiness fold consumes ─────────────────────────────────────────

#: Operand classes whose binding is GOVERNED BY POLICY — an unresolved one is a policy blocker,
#: not an ordinary binding gap.
_POLICY_OPERAND_CLASSES = frozenset({"policy_input"})


#: The temporal codes that actually BLOCK a declaration — the same pair
#: ``declarations._TEMPORAL_BLOCKING_CODES`` folds on (mirrored rather than imported: it is private
#: there, and `test_temporal_blockers_track_the_compilers_blocking_set` asserts the two agree).
#: Filtering to it means a future NON-blocking temporal annotation — an advisory the compiler adds
#: to `reason_codes` without failing the declaration — cannot silently demote a readiness the
#: compiler itself considers fine.
_TEMPORAL_BLOCKING_CODES = frozenset({
    ReasonCode.temporal_anchor_missing,
    ReasonCode.temporal_anchor_ambiguous,
})


def _temporal_blockers(plan: BindingPlanV1) -> tuple[str, ...]:
    """A compiled temporal declaration contributes only its BLOCKING codes; an absent declaration
    contributes the named absence. Nothing is invented here."""
    declaration = plan.temporal_declaration
    if declaration is None:
        return (ReasonCode.missing_temporal_declaration.value,)
    return tuple(dict.fromkeys(code.value for code in declaration.reason_codes
                               if code in _TEMPORAL_BLOCKING_CODES))


def _binding_blockers(plan: BindingPlanV1) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        code.value for binding in plan.ingredient_bindings for code in binding.reason_codes))


def _governed_policy_blockers(request: FeaturePlanningRequestV1,
                              plan: BindingPlanV1) -> tuple[str, ...]:
    """The POLICY-class operands' own verdicts: an unbound required policy operand contributes the
    planner's ``missing_required_need``; a bound one contributes whatever codes its binding
    carries. A closed vocabulary either way — never a code this module coined."""
    bound = {binding.need_role: binding for binding in plan.ingredient_bindings}
    blockers: list[str] = []
    for operand in request.operands:
        if operand.operand_class not in _POLICY_OPERAND_CLASSES and not operand.status_policy_ref:
            continue
        binding = bound.get(operand.role)
        if binding is None:
            if operand.required:
                blockers.append(ReasonCode.missing_required_need.value)
            continue
        blockers.extend(code.value for code in binding.reason_codes)
    return tuple(dict.fromkeys(blockers))


# ── the two public functions ──────────────────────────────────────────────────────────────────


def governed_requests_for_scope(conn, *, eligible_recipe_ids: frozenset[str],
                                intent_requests: Sequence[FeaturePlanningRequestV1] = (),
                                ) -> tuple[FeaturePlanningRequestV1, ...]:
    """The run's request set: the PRIMARY variant of every eligible V2 recipe, plus the caller's
    typed intent requests, deduplicated by ``planning_request_hash``.

    Dedup is by request HASH and never by canonical id: two parameter variants of one recipe are
    two requests that agree on their canonical id, and collapsing them by id would silently drop
    one of them. An eligible id the V2 registry does not carry is skipped with a log line NAMING
    it — a silently missing option is indistinguishable from an option that was never eligible.
    """
    del conn        # the V2 registry is in-code; the parameter keeps the lens's one call shape
    minted: list[FeaturePlanningRequestV1] = []
    unknown: list[str] = []
    for recipe_id in sorted(eligible_recipe_ids):
        definition = v2_recipe_by_id(recipe_id)
        if definition is None:
            unknown.append(recipe_id)
            continue
        minted.append(planning_request_from_recipe(definition))
    if unknown:
        logger.info("governed lens: %d eligible id(s) are not V2 recipes and are skipped: %s",
                    len(unknown), ", ".join(unknown))
    seen: set[str] = set()
    out: list[FeaturePlanningRequestV1] = []
    for request in [*minted, *intent_requests]:
        request_hash = planning_request_hash(request)
        if request_hash in seen:
            continue
        seen.add(request_hash)
        out.append(request)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class _PlannedRequestV1:
    """Pass 1's output for ONE request that reached a selected, resolved contract plan."""

    request: FeaturePlanningRequestV1
    plan: BindingPlanV1
    envelope: PlanEnvelopeV1


@dataclass(frozen=True, slots=True)
class _FrozenEvidenceV1:
    """Pass 2's ONE batch. Every option projects from THIS; nothing re-reads."""

    pins: dict[tuple[str, str], ResolutionPinV1]
    key_entities: dict[tuple[str, str], str | None]
    events_by_recipe: dict[str, list]


def governed_options_from_requests(conn, *, requests: Sequence[FeaturePlanningRequestV1],
                                   target_entity: str, roles: Sequence[str] = (),
                                   now: datetime, budget: CompileBudget | None = None,
                                   ) -> tuple[list[GovernedOptionV1], list[dict]]:
    """Plan every request against ONE frozen scope and split the outcomes: a SELECTED RESOLVED
    contract plan becomes a :class:`GovernedOptionV1`; anything else becomes an evidence-bearing
    rejection dict. Never raises for one bad request — a planner failure is that request's
    rejection, inside its own savepoint, and the rest of the run continues.

    The shared setup mirrors ``gate1._governed_cross_catalog_options`` exactly: ONE catalog scope,
    ONE column snapshot (only under the REPEATABLE READ feature-generation isolation, where it is
    meaningful), ONE compiler context, ONE run budget."""
    roles = tuple(roles)
    requests = tuple(requests)
    scope = resolve_catalog_scope(conn, roles=roles, target_entity=target_entity, now=now)
    column_source = (capture_column_snapshot(conn, scope.authorized_catalog_sources, roles)
                     if _on_repeatable_read(conn) else None)
    compile_ctx = build_compiler_context(conn, scope, roles, now, column_source=column_source)
    if budget is None:
        budget = CompileBudget(remaining=MAX_COMPILES_PER_RUN,
                               deadline_monotonic=time.monotonic() + COMPILE_BUDGET.total_seconds(),
                               clock=time.monotonic)

    planned: list[_PlannedRequestV1] = []
    rejections: list[dict] = []
    for request in requests:
        try:
            with conn.transaction():    # per-request savepoint: one planner DB error is one
                result = plan_planning_request(  # rejection, never a poisoned transaction
                    conn, request=request, target_entity=target_entity, scope=scope, roles=roles,
                    now=now, compile_ctx=compile_ctx, budget=budget)
        except Exception:
            logger.exception("governed planning failed for request %s",
                             request.source_definition_id)
            rejections.append({"lens": _LENS, "reason": ReasonCode.planner_internal_error.value,
                               "recipe_id": request.source_definition_id})
            continue
        plan = _selected_resolved_plan(result)
        envelope = plan_envelope_from_result(result) if plan is not None else None
        if plan is None or envelope is None:
            # A resolved contract ALWAYS projects an envelope; if it cannot, fail closed.
            rejections.append(_rejection(request, result))
            continue
        planned.append(_PlannedRequestV1(request=request, plan=plan, envelope=envelope))

    if not planned:
        return [], rejections
    evidence = _load_evidence(conn, planned)
    options = [_option_from(entry, evidence=evidence, compile_ctx=compile_ctx,
                            target_entity=target_entity)
               for entry in planned]
    return options, rejections


# ── pass 1 helpers ────────────────────────────────────────────────────────────────────────────


def _on_repeatable_read(conn) -> bool:
    """True when this connection runs under the REPEATABLE READ feature-generation isolation the
    C0 column snapshot requires. Mirrors ``gate1._on_repeatable_read`` (a private there)."""
    return conn.execute("SHOW transaction_isolation").fetchone()[0] == _REPEATABLE_READ


def _selected_resolved_plan(result: BindingPlanningResultV1) -> BindingPlanV1 | None:
    if (result.contract_result_status is not ContractResolutionStatus.resolved
            or result.selected_contract_physical_plan_id is None):
        return None
    return next((p for p in result.candidate_plans
                 if p.physical_plan_id == result.selected_contract_physical_plan_id), None)


def _rejection_reason(result: BindingPlanningResultV1) -> str:
    """The primary reason this request has no SELECTED RESOLVED governed contract — the same
    precedence ``gate1._governed_rejection_reason`` applies: the selected plan's contract reason,
    else the fail-closed source→target REJECT reason, else a result-level assembler reason (the
    tier-1 selection reasons say nothing about the cross-catalog outcome and are stripped), else
    the observed contract status."""
    pid = result.selected_contract_physical_plan_id
    if pid is not None:
        plan = next((p for p in result.candidate_plans if p.physical_plan_id == pid), None)
        if plan is not None and plan.contract_primary_reason_code is not None:
            return plan.contract_primary_reason_code.value
    for plan in result.candidate_plans:
        if (plan.path_resolution_status is PathResolutionStatus.source_to_target_rejected
                and plan.primary_reason_code is not None):
            return plan.primary_reason_code.value
    cross = [code for code in result.reason_codes
             if code not in (ReasonCode.selected_best_single_catalog,
                             ReasonCode.ambiguous_multiple_equal_plans)]
    if cross:
        return cross[0].value
    return result.contract_result_status.value


def _rejection(request: FeaturePlanningRequestV1, result: BindingPlanningResultV1) -> dict:
    """The evidence-bearing refusal. ``unmet_hops`` is forward-compatible: the planner does not
    carry ``unmet_hop`` on a rejected candidate yet, so today this reads empty rather than
    pretending the hops are known."""
    unmet = [hop for hop in (getattr(plan, "unmet_hop", None)
                             for plan in result.candidate_plans
                             if plan.path_resolution_status
                             is PathResolutionStatus.source_to_target_rejected)
             if hop is not None]
    return {"lens": _LENS, "reason": _rejection_reason(result),
            "recipe_id": request.source_definition_id,
            "request_hash": request.source_content_hash,
            "evidence": [], "unmet_hops": unmet}


# ── pass 2: ONE batch, then project ───────────────────────────────────────────────────────────


def _logical_ref(catalog_source: str, object_ref: str) -> str:
    """The evidence store's SCHEMA-PRESERVING logical ref for a graph ``(catalog, object_ref)``.

    ``graph_node.object_ref`` is stored PUBLIC-FLATTENED (``public.table.column``), which is the
    spelling ``field_evidence`` is keyed by for a public-schema source. A source whose real schema
    is not ``public`` keys its evidence under that schema (``graph_node.schema_name``), and
    recovering it costs one graph row per ref — precisely the N+1 this builder forbids. Known
    limitation, carried openly: for such a source the pin lookup misses and the binding's
    authority reads ``absent``, which is the fail-closed direction (never a fabricated authority).
    """
    parts = object_ref.split(".")
    if len(parts) >= 3:
        return normalize_ref(catalog_source, parts[0], parts[-2], parts[-1])
    if len(parts) == 2:
        return normalize_ref(catalog_source, "public", parts[0], parts[1])
    return normalize_ref(catalog_source, "public", object_ref)


def _read_set_pairs(plan: BindingPlanV1) -> tuple[tuple[str, str], ...]:
    """The FULL qualified physical read set — every column the contract would touch (ingredients,
    join/bridge keys, anchors), falling back to the ingredient bindings when a plan carries no
    read set. Deduped + sorted so the idea is deterministic."""
    if plan.physical_read_set is not None and plan.physical_read_set.columns:
        pairs = {(c.catalog_source, c.object_ref) for c in plan.physical_read_set.columns}
    else:
        pairs = {(b.bound_catalog_source, b.bound_object_ref) for b in plan.ingredient_bindings}
    return tuple(sorted(pairs))


def _load_evidence(conn, planned: Sequence[_PlannedRequestV1]) -> _FrozenEvidenceV1:
    """The THREE batched reads — one each, over the union of every resolved plan.

    The review read is issued only when a RECIPE-origin request resolved: it is a whole-table
    batch rather than a per-definition lookup, but a run carrying no recipe origin has no reason
    to touch the review store at all (origin purity)."""
    logical_refs: list[str] = []
    for entry in planned:
        logical_refs.extend(_logical_ref(catalog, ref)
                            for catalog, ref in _read_set_pairs(entry.plan))
    grain_refs = [entry.plan.output_grain_ref for entry in planned
                  if entry.plan.output_grain_ref is not None]
    pins = current_resolution_pins(
        conn, logical_refs=list(dict.fromkeys(logical_refs)), fields=("concept",))
    key_entities = key_entities_for(conn, grain_refs)
    events = (review_events_all(conn)
              if any(entry.request.origin == DEFINITION_ORIGIN_RECIPE_V2 for entry in planned)
              else {})
    return _FrozenEvidenceV1(pins=pins, key_entities=key_entities, events_by_recipe=events)


def _authority(pin: ResolutionPinV1 | None) -> str:
    """D4 — the authority a binding may claim: the WINNING evidence's producer/strength, and only
    while the resolver itself says the field is resolved AND load-bearing. A conflicted or
    display-only pin is ``absent``: an unresolved field has no authority to lend."""
    if (pin is not None and pin.producer and pin.conflict_state == "resolved"
            and pin.load_bearing):
        return f"{pin.producer}/{pin.strength}"
    return "absent"


def _role_bindings(plan: BindingPlanV1,
                   pins: dict[tuple[str, str], ResolutionPinV1]) -> tuple[RoleBinding, ...]:
    bindings = []
    for binding in plan.ingredient_bindings:
        pin = pins.get(
            (_logical_ref(binding.bound_catalog_source, binding.bound_object_ref), "concept"))
        bindings.append(RoleBinding(
            role=binding.need_role,
            ref=(binding.bound_catalog_source, binding.bound_object_ref),
            authority=_authority(pin),
            confirmation_required=False,
            evidence_ids=(pin.evidence_id,) if pin is not None and pin.evidence_id else ()))
    return tuple(bindings)


def _class_refs(request: FeaturePlanningRequestV1, plan: BindingPlanV1,
                operand_class: str) -> tuple[tuple[str, str], ...]:
    """The qualified refs of the bindings whose OPERAND declares this class — the request's own
    typed declaration, never a guess from the column."""
    roles = {op.role for op in request.operands if op.operand_class == operand_class}
    return tuple((b.bound_catalog_source, b.bound_object_ref)
                 for b in plan.ingredient_bindings if b.need_role in roles)


def _window_value(request: FeaturePlanningRequestV1) -> str | None:
    for name, value in request.parameter_values:
        if name == "window":
            return str(value)
    return None


def _grain_table(plan: BindingPlanV1, *, target_entity: str,
                 key_entities: dict[tuple[str, str], str | None]) -> str | None:
    """The landing TABLE of the plan's output grain — carried only while the governed key-entity
    read CONFIRMS that the grain key names the target entity. An unconfirmed grain leaves the
    field absent: the plan's own ``output_grain_ref`` still stands as the planner's fact, but this
    module never restates it as a governed confirmation it does not have."""
    grain = plan.output_grain_ref
    if grain is None:
        return None
    entity = key_entities.get(grain)
    if entity is None or entity.strip().lower() != target_entity.strip().lower():
        return None
    return table_of(grain[1])


def _option_from(entry: _PlannedRequestV1, *, evidence: _FrozenEvidenceV1, compile_ctx,
                 target_entity: str) -> GovernedOptionV1:
    """Project ONE option from the frozen batch. Pure over ``evidence`` — no read happens here."""
    request, plan, envelope = entry.request, entry.plan, entry.envelope
    governance, definition = _governance_for(request, events_by_recipe=evidence.events_by_recipe)
    display_name, business_definition = _display_for(request, definition)
    identity = _variant_identity_for(request, plan, compile_ctx=compile_ctx)
    readiness = fold_request_readiness(
        request, governance,
        temporal_blockers=_temporal_blockers(plan),
        binding_blockers=_binding_blockers(plan),
        governed_policy_blockers=_governed_policy_blockers(request, plan))
    requirements, unmapped = _project_requirements(_reason_contexts(plan))

    pairs = _read_set_pairs(plan)
    declaration = plan.temporal_declaration
    time_ref = None
    if (declaration is not None and declaration.anchor_catalog_source
            and declaration.anchor_binding):
        time_ref = (declaration.anchor_catalog_source, declaration.anchor_binding)
    result_class = request.formula.result_class if request.formula is not None else ""
    idea = FeatureIdea(
        name=display_name,
        description=business_definition,
        derives_from=[ref for _catalog, ref in pairs],
        aggregation=(request.formula.result_class if request.formula is not None
                     else "conceptual"),
        grain_table=_grain_table(plan, target_entity=target_entity,
                                 key_entities=evidence.key_entities),
        derives_pairs=pairs,
        verification="DESIGN-CHECKED",
        rationale=(f"governed cross-catalog plan for {request.source_definition_id} at "
                   f"{target_entity} grain")[:_MAX_RATIONALE],
        operation_kind=request.computation_kind,
        measure_refs=_class_refs(request, plan, "measure"),
        grain_refs=(plan.output_grain_ref,) if plan.output_grain_ref else (),
        time_ref=time_ref,
        window=_window_value(request),
        operation_class=result_class,
        grouping_refs=_class_refs(request, plan, "dimension"),
        validation_status="DESIGN_CHECKED",
        requirements=requirements,
        plan_envelope=envelope,
        origin="governed_planner",
        path_authority="governed_cross_catalog",
        generation_source=("recipe" if request.origin == DEFINITION_ORIGIN_RECIPE_V2
                           else "llm_intent"),
        # ORIGIN PURITY: only a recipe origin wears the recipe badge; every origin carries the
        # neutral definition id.
        recipe_id=(request.source_definition_id
                   if request.origin == DEFINITION_ORIGIN_RECIPE_V2 else None),
        source_definition_id=identity.canonical_definition_id,
        input_role_bindings=_role_bindings(plan, evidence.pins),
        planner_applicability="applicable_cross_catalog",
        physical_plan_id=envelope.physical_plan_id)
    return GovernedOptionV1(
        idea=idea, request=request, identity=identity, governance=governance,
        readiness=readiness, display_name=display_name,
        business_definition=business_definition, unmapped_requirement_codes=unmapped)


__all__ = [
    "REQUIREMENT_BUILDER_CODES",
    "DefinitionGovernanceStateV1",
    "GovernedOptionV1",
    "ReasonContextV1",
    "governed_options_from_requests",
    "governed_requests_for_scope",
]
