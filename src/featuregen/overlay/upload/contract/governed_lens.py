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

WHERE THE CROSS-CATALOG FRONTIER STANDS (re-measured 2026-08-23, after S1A-4c ``1c656743``). Two
planner gaps remain between a V2 RECIPE and a resolved governed cross-catalog contract. Neither is
in this module, and this module may not fix them (it owns no planner file):

* **G1 — CLOSED.** ``planning_probe`` no longer projects the operands' ``join_role`` /
  ``temporal_role`` verbatim: it DECLARES them at projection time from the request's own facts —
  the V2 ``operand_class`` vocabulary plus the concept registry's ``entity_link`` / ``pit_role``.
  Almost no operand in the 317-recipe registry declares a role — 7 of 1196 do, and every one of
  them is a G2 RULING (A6's on the identifier-valued ``transaction``/``original_txn`` slots, A6b's
  on the count recipe's transaction identity), never a compensation for the projection — yet a
  request built by ``planning_request_from_recipe`` ALONE yields
  ``account → source_entity_key`` and its event clock ``→ time/event_time``,
  ``plan._assemble_rollups`` starts its frontier, and a roll-up spanning both catalogs is
  assembled. ``test_as_shipped_recipe_request_reaches_the_g3_boundary`` pins the shipped request's
  whole projection.
* **G3 — the LIVE boundary.** ``plan_bindings`` never runs
  ``assembly.attach_executable_bridge_realizations``, and ``build_compiler_context`` correctly
  leaves ``allow_provisional_bridge_cardinality`` false (it is sandbox-only), so a governed BRIDGE
  hop's physical cardinality is UNAVAILABLE and any measure staged there fails
  ``physical_cardinality_unavailable``. That — not a discovery failure — is what a shipped
  recipe-origin request refuses with today.
* **G2 — real, and MASKED BEHIND G3.** ``need_metadata._derive_one`` maps an operand with no
  ``entity_link`` and no ``pit_role`` — a ``status``, a ``dimension``, a ``direction`` — to
  ``JoinRole.MEASURE``, so the contract fails for an operand nobody intended to aggregate. It is
  invisible on a bridge hop because ``compile_aggregation`` short-circuits on ``card is None``
  (``declarations.py``) BEFORE the additivity matrix runs; it surfaces the moment a cardinality is
  available, which today happens on an INTRA-catalog realization hop (whose declared join supplies
  one). ``test_the_measured_refusal_sequence_is_g3_before_g2`` and
  ``test_g2_surfaces_only_once_a_cardinality_is_available`` pin both halves.

**Both are CHARTERED, not fixed here.** ``assembly.attach_executable_bridge_realizations`` has zero
callers and attaching a revision moves SEGMENT identity, so G3 is a follow-on decided at the
Stage-1C report with the realization-gap queue's evidence in hand — deliberately not ridden along
with a projection change. G2 rides the same charter and carries an OPERAND WORKLIST: the
class-keyed projection and ``_derive_one``'s concept-keyed ladder DISAGREED on 82 of the 1195 V2
operands when first measured (at ``1c656743``) — 63 value-classed operands on an entity-linked
concept, 17 on a pit-bearing one, and ``device_sharing_velocity``'s two. The rulings taken since
(A6's, A6b's) moved that census to 76 across 71 recipes; the gate's own test is where it is
PINNED, and any count quoted in prose is a snapshot rather than the authority.
``test_the_class_keyed_projection_diverges_from_the_concept_ladder_only_where_g2_lives``
(``planner/test_requests.py``) pins that divergence BY SHAPE in both directions, so the worklist
G2's ruling has to decide stays self-maintaining rather than rotting silently.

The consequence for consumers: a request whose operands reach the planner with binding roles — the
projection now derives them for ANY request, and an LLM intent may also declare its own — assembles
a cross-catalog plan today. Whether it RESOLVES depends on what it stages over the bridge hop: a
key/time-only contract resolves; a contract staging a MEASURE across the governed bridge becomes an
evidence-bearing REJECTION naming ``physical_cardinality_unavailable``, which is the honest answer
until G3 closes.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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
from featuregen.overlay.upload.field_resolution import (
    ResolutionPinV1,
    current_resolution_pins,
    pin_authority,
)
from featuregen.overlay.upload.object_ref import normalize_ref, qualify_object_ref
from featuregen.overlay.upload.planner.contracts import (
    BindingPlanningResultV1,
    BindingPlanV1,
    ContractResolutionStatus,
    PathResolutionStatus,
    ReasonCode,
    UnmetHopV1,
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
    # S1B-3: the SELECTED plan's own facts, from the SAME `_plan_facts` derivation a rejection
    # carries. Deliberately shared rather than re-derived per outcome: a ledger that answers
    # "which catalog anchored this?" or "how many fan-in hops?" differently depending on whether
    # the request resolved is a ledger whose columns mean two things. The `FeatureIdea` and its
    # envelope cannot supply them honestly — the envelope's `ordered_path` is a display string and
    # `derives_pairs` is a SORTED read set, whose first entry is the alphabetically-first catalog
    # rather than the anchor. Defaulted so no other constructor moves.
    plan_facts: dict = field(default_factory=dict)
    # C2a: the SELECTED RESOLVED plan this option was projected from, carried rather than
    # re-planned. The serving lane must mint the option's LOGICAL identity
    # (:func:`~planner.logical_resolution.resolve_logical_plan`) inside the same request, and that
    # derivation needs the plan OBJECT — ``plan_facts`` is a summary and the envelope's
    # ``ordered_path`` is a display string, so a consumer handed only those would have to re-run
    # the planner to recover what this builder already held. Defaulted so no constructor moves.
    plan: BindingPlanV1 | None = None


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
    return _governed_variant_identity(
        request,
        physical_plan_content_hash=plan_content_hash(
            compile_ctx, plan, planning_probe(request)),
        parameter_binding_hash=_parameter_binding_hash(request))


def plan_content_hash(compile_ctx, plan: BindingPlanV1, template) -> str:
    """THE ``physical_plan_content_hash`` rule, for any lane that holds a compiled plan.

    The compiled plan's ``contract_input_hash`` — the full 64-hex digest of the inputs the verdict
    consumed — when the plan actually compiled, and the untruncated ``full_physical_plan_hash``
    otherwise. Never the truncated ``bp_…`` display id, which is the trap
    :func:`_governed_variant_identity` refuses: a well-formed string that mints a well-formed but
    WRONG variant id.

    ``template`` is whatever ``contract_input_hash`` reads a recipe's identity from — the telemetry
    lane projects the request's own ``planning_probe``; gate1's live lane holds the real
    ``Template`` the planner compiled. Shared so a resolved row means the same thing in both."""
    compiled = (plan.path_resolution_status is PathResolutionStatus.source_to_target_resolved
                and plan.contract_resolution_status is not ContractResolutionStatus.not_compiled)
    return (contract_input_hash(compile_ctx, plan, template) if compiled
            else full_physical_plan_hash(plan))


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
            # The SAME key set as every other rejection, blank — a consumer must never branch on
            # which rejection shape it is holding to find out whether a field exists.
            rejections.append({"lens": _LENS,
                               "recipe_id": request.source_definition_id,
                               "request_hash": request.source_content_hash,
                               "planning_request_hash": planning_request_hash(request),
                               **rejection_evidence(None)})
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
    """The primary reason this request has no SELECTED RESOLVED governed contract — THE one
    precedence, for both lanes (gate1's live Template lane imports this rather than keeping the
    copy it once had): the selected plan's contract reason, else the fail-closed source→target
    REJECT reason, else a result-level assembler reason (the tier-1 selection reasons say nothing
    about the cross-catalog outcome and are stripped), else the observed contract status."""
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


def _best_plan(result: BindingPlanningResultV1) -> BindingPlanV1 | None:
    """The plan a rejection is ABOUT, under the SAME precedence :func:`_rejection_reason` uses.

    Kept as its own function so the reason and the facts beside it can never come from two
    different plans: the selected contract plan (the one whose contract refused), else the
    fail-closed source→target REJECT, else the highest-tier candidate, else nothing at all — a
    result with no candidates, or a planner failure, has no plan to describe."""
    pid = result.selected_contract_physical_plan_id
    if pid is not None:
        selected = next((p for p in result.candidate_plans if p.physical_plan_id == pid), None)
        if selected is not None:
            return selected
    rejected = next((p for p in result.candidate_plans
                     if p.path_resolution_status
                     is PathResolutionStatus.source_to_target_rejected), None)
    if rejected is not None:
        return rejected
    return max(result.candidate_plans, key=lambda p: p.bridge_count, default=None)


def _segment_evidence(plan: BindingPlanV1) -> list[dict]:
    """The plan's ordered path segments, JSON-NATIVE — no enums, no ``None``, nothing a jsonb
    column would have to be taught about.

    This is the ONLY carrier for the contract-level realization demand. A ``physical_cardinality_
    unavailable`` refusal rides a path that RESOLVED source→target, so it mints no rejected
    candidate and has no unmet hop — the crossing that needs work is nameable ONLY from the
    segments the resolved path actually used. ``has_realization_revision`` is the one derived
    field: it is exactly the question the G3 boundary asks, and a consumer must not have to know
    that ``bridge_realization_revision is None`` is what "not executable" looks like.

    ``relationship_id`` / ``relationship_version`` are read from the segment and left as the empty
    string when the assembler did not stamp them (measured: it stamps them on the semantic rollup,
    not on the governed bridge that realizes it). Nothing here infers one segment's relationship
    from its neighbour — an inferred crossing identity is a fabricated demand."""
    return [{
        "segment_kind": str(segment.segment_kind),
        "catalog_source": segment.catalog_source or "",
        "from_entity": segment.from_entity or "",
        "to_entity": segment.to_entity or "",
        "relationship_id": segment.relationship_id or "",
        "relationship_version": segment.relationship_version or "",
        "bridge_fact_key": segment.bridge_fact_key or "",
        "realization_ref": segment.realization_ref or "",
        "cardinality": str(segment.cardinality or ""),
        "bridge_from_catalog_source": segment.bridge_from_catalog_source or "",
        "bridge_from_object_ref": segment.bridge_from_object_ref or "",
        "bridge_to_catalog_source": segment.bridge_to_catalog_source or "",
        "bridge_to_object_ref": segment.bridge_to_object_ref or "",
        "has_realization_revision": segment.bridge_realization_revision is not None,
    } for segment in plan.path_segments]


def _plan_facts(plan: BindingPlanV1 | None) -> dict:
    """A plan's own facts under the names ``governed_planning_observation`` spells them.

    **THE one derivation, for BOTH outcomes** — a rejection's best plan (:func:`_best_plan`) and a
    resolved option's selected plan (``GovernedOptionV1.plan_facts``). Sharing it is the point: a
    ledger whose ``anchor_catalog_source`` or ``hop_count`` means one thing for a resolved row and
    another for a refused one cannot be grouped, and the divergence would be invisible on any seed
    whose catalogs happen to sort in path order.

    ``anchor_catalog_source`` is ``plan.catalog_source`` — where the path STARTS. Neither
    ``derives_pairs`` (a sorted read set) nor the envelope's ``ordered_path`` (a display string)
    is that fact; the first is alphabetical and the second is parsed prose.

    ``hop_count`` is the number of AGGREGATION hops the contract compiled — the count a demand
    queue means by "hops". The path segment count includes the direct-catalog anchor and the
    bridges, and is not it.

    ``participating_catalogs`` is the plan's own tuple, i.e. PATH order (the anchor leads), not
    sorted. Sorting would throw away the traversal for a tidiness no consumer asked for, and a
    reader can still compare two lists as sets.

    All-blank when there is no plan (a planner failure, an empty candidate set), so a consumer
    never branches on which shape it is holding."""
    if plan is None:
        return {"physical_plan_id": "", "contract_id": "", "anchor_catalog_source": "",
                "participating_catalogs": [], "hop_count": 0, "bridge_count": 0,
                "evidence": [], "reason_codes": []}
    codes = [*(code.value for code in plan.contract_reason_codes),
             *(code.value for code in plan.reason_codes)]
    return {
        "physical_plan_id": plan.physical_plan_id,
        "contract_id": plan.contract_id or "",
        # The catalog the path STARTS from — `plan.catalog_source` — which is what the ledger's
        # `anchor_catalog_source` means and what S1B-1 asked the adapter to pass explicitly rather
        # than let the store look up.
        "anchor_catalog_source": plan.catalog_source or "",
        "participating_catalogs": list(plan.participating_catalogs),
        "hop_count": len(plan.hop_aggregations),
        "bridge_count": plan.bridge_count,
        "evidence": _segment_evidence(plan),
        "reason_codes": list(dict.fromkeys(codes)),
    }


def _unmet_hop_demand(hop: UnmetHopV1) -> dict:
    """ONE unmet hop in the DEMAND MATERIAL shape ``governed_observation_store``'s
    ``record_bridge_demand`` reads — its ``_HOP_TEXT_FIELDS`` plus ``hop_index``, ``verdict`` and
    the two JSONB evidence lists, under exactly those names and nothing else.

    JSON-native throughout (the two lists become ``jsonb`` columns), so the ledger can persist this
    without knowing the planner's dataclasses. ``cardinality`` and ``position_entity`` are
    deliberately NOT serialized: ``bridge_demand_observation`` has no column for either, and the
    store would silently drop them — they stay on the typed ``UnmetHopV1`` where a reader who
    wants them can find them. ``to_endpoint_hint`` stays out entirely (S1B-1's advisory column,
    never the planner's to fill)."""
    return {"relationship_id": hop.relationship_id,
            "relationship_version": hop.relationship_version,
            "from_entity": hop.from_entity, "to_entity": hop.to_entity,
            "position_catalog": hop.position_catalog,
            "position_table_ref": hop.position_table_ref,
            "hop_index": hop.hop_index, "verdict": hop.verdict,
            "realizers": [{"catalog_source": r.catalog_source,
                           "to_object_ref": r.to_object_ref,
                           "from_key_ref": r.from_key_ref,
                           "to_key_ref": r.to_key_ref} for r in hop.realizers],
            "near_side_key_refs": list(hop.near_side_key_refs)}


def resolution_evidence(plan: BindingPlanV1) -> dict:
    """The RESOLVED counterpart of :func:`rejection_evidence`, from the SELECTED plan.

    Same keys, same ``_plan_facts`` derivation, so a caller writing an observation row never
    branches on the outcome to find out whether a field exists. Two of them are empty BY RULING
    rather than by accident:

    * ``reason_codes`` — a resolved row records no refusal. The plan's own observed codes stay on
      the plan; a reason code standing beside ``resolution_status = resolved`` reads as a refusal
      that somehow resolved.
    * ``unmet_hops`` — nothing dead-ended, so there is no demand to file. A resolved run is not
      somebody's missing crossing, and filing one would report demand for work nobody needs.
    """
    facts = _plan_facts(plan)
    facts["reason_codes"] = []
    return {"reason": "", **facts, "unmet_hops": []}


def rejection_evidence(result: BindingPlanningResultV1 | None) -> dict:
    """THE evidence half of a refusal — the headline reason, the best plan's own facts, its ordered
    path segments and its unmet hops — with NO request-identity fields on it at all.

    **One derivation, three writers.** :func:`_rejection` composes it with the request's identity
    for the telemetry lane; ``gate1._governed_cross_catalog_options`` records it verbatim for the
    LIVE lane (which plans a ``Template`` and holds no ``FeaturePlanningRequestV1``, so it cannot
    call ``_rejection`` at all); and a planner explosion passes ``None`` for the all-blank shape.
    Splitting it out is what stops the live lane from growing a second, drifting copy of the two
    demand sources' input contract.

    ``result=None`` is the planner-failure shape: every key present, every value blank, so a
    consumer never branches on which shape it is holding to find out whether a field exists.
    """
    if result is None:
        failure = ReasonCode.planner_internal_error.value
        return {"reason": failure, **_plan_facts(None), "reason_codes": [failure],
                "unmet_hops": []}
    unmet: list[dict] = []
    seen: set[str] = set()
    for plan in result.candidate_plans:
        if (plan.path_resolution_status is not PathResolutionStatus.source_to_target_rejected
                or plan.unmet_hop is None):
            continue
        demand = _unmet_hop_demand(plan.unmet_hop)
        marker = json.dumps(demand, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        unmet.append(demand)
    reason = _rejection_reason(result)
    facts = _plan_facts(_best_plan(result))
    # The headline leads the codes and is never duplicated inside them: a ledger row's
    # `reason_codes[0]` is the answer to "why did this refuse?", and the rest is what else the
    # plan observed on the way.
    facts["reason_codes"] = [reason, *(c for c in facts["reason_codes"] if c != reason)]
    return {"reason": reason, **facts, "unmet_hops": unmet}


def _rejection(request: FeaturePlanningRequestV1, result: BindingPlanningResultV1) -> dict:
    """The evidence-bearing refusal. ``unmet_hops`` carries one entry per source→target-REJECTED
    candidate that named the hop it died on (S1B-2). It stays EMPTY, honestly, for a refusal that
    has no unmet hop behind it at all — a contract-compile refusal on a path that DID resolve
    source→target mints no rejected candidate, and the frontier-truncation reject deliberately
    names no hop.

    **``reason`` and ``unmet_hops`` are different questions, and an adapter must not conflate
    them.** ``reason`` is the ONE headline for why this request has no governed contract, chosen by
    ``_rejection_reason``'s precedence — which prefers the SELECTED plan's contract refusal. A run
    can therefore hand back a contract-refusal headline (say
    ``physical_cardinality_unavailable``) while other candidates in the same run dead-ended and are
    carrying real, demand-bearing hops. Both are true at once. Anything filing demand rows must key
    off each hop's OWN ``verdict``, never off the rejection's ``reason``.

    **The ``planner_capacity`` queue is asymmetric, by design and permanently.** Both capacity
    verdicts route to the same queue in ``governed_observation_store.DEMAND_VERDICT_QUEUES``, but
    only one of them can ever arrive through this list: ``bounded_out_max_bridges`` names the
    crossing it could not afford, so its reject carries a populated hop and appears here;
    ``bounded_out_max_frontier_states`` is minted from the START state, which realized nothing, so
    it carries ``unmet_hop=None`` and is structurally invisible to ``unmet_hops``. A consumer that
    wants frontier-exhaustion counted must read it from the run's bounding metrics or file it from
    the rejection level — it will never appear here, and its absence is not a defect to fix.

    Identical hop dicts are collapsed: two rejected candidates can dead-end on the SAME hop at the
    SAME position by different routes (the diamond case), and that is one demand, not two. The
    store would dedupe them anyway on ``demand_identity_hash``; doing it here keeps the payload
    honest about how many distinct demands were actually found. Order is preserved. (Both the
    collapse and the reason/facts derivation live in :func:`rejection_evidence`, which the LIVE
    lane shares.)"""
    return {"lens": _LENS,
            "recipe_id": request.source_definition_id,
            "request_hash": request.source_content_hash,
            # The REQUEST's own identity. `recipe_id` + `request_hash` are both definition-level:
            # two parameter variants of one recipe agree on BOTH, so neither maps a rejection back
            # to the request that produced it. This does.
            "planning_request_hash": planning_request_hash(request),
            **rejection_evidence(result)}


# ── pass 2: ONE batch, then project ───────────────────────────────────────────────────────────


def _logical_ref(catalog_source: str, object_ref: str) -> str:
    """The evidence store's SCHEMA-PRESERVING logical ref for a graph ``(catalog, object_ref)``.

    ``graph_node.object_ref`` is stored PUBLIC-FLATTENED (``public.table.column``), which is the
    spelling ``field_evidence`` is keyed by for a public-schema source. A source whose real schema
    is not ``public`` keys its evidence under that schema (``graph_node.schema_name``), and
    recovering it costs one graph row per ref — precisely the N+1 this builder forbids. Known
    limitation, carried openly: for such a source the pin lookup misses and the binding's
    authority reads ``absent``, which is the fail-closed direction (never a fabricated authority).

    The reconstruction itself is ``object_ref.qualify_object_ref`` — shared, because the
    bridge-demand queue's endpoint sample needs the identical answer and a second copy could drift.
    """
    return qualify_object_ref(catalog_source, object_ref)


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


def _role_bindings(plan: BindingPlanV1,
                   pins: dict[tuple[str, str], ResolutionPinV1]) -> tuple[RoleBinding, ...]:
    bindings = []
    for binding in plan.ingredient_bindings:
        pin = pins.get(
            (_logical_ref(binding.bound_catalog_source, binding.bound_object_ref), "concept"))
        bindings.append(RoleBinding(
            role=binding.need_role,
            ref=(binding.bound_catalog_source, binding.bound_object_ref),
            # S1A-5b (ruling): ONE authority law, shared with the durable write's C2 floors. The
            # verbatim D4 clause that used to live here (`conflict_state == "resolved" and
            # load_bearing`) was INERT against real data — `concept` is a RECOMMENDATION-tier
            # policy, so no concept pin is ever either, and every genuinely human-confirmed
            # binding read `absent`. `pin_authority` documents why in full.
            authority=pin_authority(pin),
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


def fold_governed_binding_plan(idea: FeatureIdea) -> dict | None:
    """S1A-5b — the governed cross-catalog plan, as the FROZEN dict a decision row stores.

    The sibling of ``recipe_planning_lens.fold_frozen_binding_plan`` (the single-source class) and
    deliberately the SAME shape where the two overlap — ``plan_kind``, ``read_set``,
    ``role_bindings``, ``grain_refs``, ``output_grain`` — so one loader reads both and
    ``activation_policy`` needs no second vocabulary.

    Two things differ, and both are the point:

    * **every read-set entry is FULLY QUALIFIED** (``source::schema.table.column``). A single-source
      plan stores bare refs and one plan-wide ``catalog_source`` every entry inherits; a
      cross-catalog plan has no single catalog to inherit from, so an entry that did not name its
      own would be attributed to whichever catalog the plan happened to mention — measuring the
      authority of columns that do not exist and calling the answer an execution floor. This is the
      dialect ``semantic_option_decision._governed_read_set_pairs`` parses STRICTLY, so a spelling
      that is not canonical is refused at load rather than silently mis-attributed;
    * **``catalog_sources``, ``ordered_path``, ``physical_plan_id`` and the bridge realization
      dependencies ride along**: the envelope's own record of which catalogs the plan spans, in
      what order, over which governed hops. A single-source plan has nothing to say there.

    ``None`` when the idea carries no plan envelope — an option with no governed plan has no
    governed plan to freeze, and an empty dict would read as one that authorizes nothing.

    Raises:
        ValueError: a ref with fewer than three dotted components, from EITHER source this fold
            qualifies — ``derives_pairs`` (the read set) or ``input_role_bindings`` (the role map).
            The two are separate projections of the plan (the read set comes from
            ``physical_read_set``, the bindings from ``ingredient_bindings``) and neither is
            guaranteed to be a subset of the other, so checking only one would leave the other
            able to mis-split. A table-level ref would mis-split (``"transactions.acct"`` →
            ``schema="transactions", table="acct"`` with the column silently lost) into a
            well-formed qualified ref naming a different object. That is an upstream defect, and it
            must name itself rather than be attributed.
    """
    env = idea.plan_envelope
    if env is None:
        return None
    # ONE pre-pass over BOTH sources, before any key is built: the fold is all-or-nothing, so a
    # partially-built plan dict can never escape past a refusal.
    checked = [("read set", ref) for _catalog, ref in idea.derives_pairs]
    checked += [(f"role {b.role!r}", b.ref[1]) for b in idea.input_role_bindings if b.ref]
    for source, ref in checked:
        if len(ref.split(".")) < 3:
            raise ValueError(
                f"governed {source} ref {ref!r} has fewer than three dotted components: the "
                f"planner's read sets and role bindings are column-level (schema.table.column), "
                f"and a table-level ref reaching this fold would mis-split into a qualified ref "
                f"naming a different object")
    return {
        "plan_kind": "governed_cross_catalog",
        "catalog_sources": sorted(set(env.catalog_sources)),
        "read_set": sorted(normalize_ref(catalog, *ref.split(".")[-3:])
                           for catalog, ref in idea.derives_pairs),
        "role_bindings": {b.role: normalize_ref(b.ref[0], *b.ref[1].split(".")[-3:])
                          for b in idea.input_role_bindings if b.ref},
        "grain_refs": [[catalog, ref] for catalog, ref in idea.grain_refs],
        "ordered_path": list(env.ordered_path),
        "output_grain": env.target_entity or "",
        "physical_plan_id": env.physical_plan_id,
        "bridge_realization_dependencies": [dict(d) for d in env.bridge_realization_dependencies],
    }


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
        business_definition=business_definition, unmapped_requirement_codes=unmapped,
        plan_facts=_plan_facts(plan), plan=plan)


__all__ = [
    "REQUIREMENT_BUILDER_CODES",
    "DefinitionGovernanceStateV1",
    "GovernedOptionV1",
    "ReasonContextV1",
    "fold_governed_binding_plan",
    "governed_options_from_requests",
    "governed_requests_for_scope",
    "plan_content_hash",
    "rejection_evidence",
    "resolution_evidence",
]
