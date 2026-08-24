"""SE-8 (part 1) — the planner's origin-neutral entry: plan a FeaturePlanningRequestV1.

The physical planner (discovery → enumeration → ordering → cross-catalog roll-ups → the plan
envelope) was built Template-first. This module is the generalization seam the plan requires:
`plan_planning_request` accepts the SE-1 origin-neutral request — recipe, LLM intent, or user
definition — projects its typed operands into the planner's own probe vocabulary, and runs the
UNCHANGED pipeline, so every origin gets the same source selection, the same realization paths,
the same fail-closed ambiguity handling and the same replay envelope. No parallel planner
exists; the probe is a projection, never a second model.

The projection is honest about what each side owns: `allowed_source_grains`,
`distinct_binding_group` and `alternative_concepts` carry over verbatim; `join_role` /
`temporal_role` are DECLARED at projection time from the request's own facts (S1A-4c —
`_projected_roles`); parameters flatten to their RESOLVED values (a request is one variant by
contract).

Deeper SE-8 work (feature-level dataset decisions, event-required operands refusing
current-only snapshot sources, profile-gated source selection) lands INSIDE the pipeline in
later parts — this seam is what lets those rules serve every origin at once when they do.
"""
from __future__ import annotations

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
)

# `_PIT_ROLE_TO_TEMPORAL` is the ONE governed pit_role -> TemporalRole vocabulary map, imported
# rather than restated: the projection must declare the SAME temporal role `_derive_one` derives,
# or a probe and a legacy template would disagree about what a concept's time means.
from featuregen.overlay.upload.need_metadata import _PIT_ROLE_TO_TEMPORAL
from featuregen.overlay.upload.templates import Need, Template

# The V2 `operand_class` vocabulary (`recipe_contract_v2.OPERAND_CLASSES`, 8 closed values)
# projected onto the planner's OWN `JoinRole` vocabulary (5 closed values). `entity_key` is not
# listed because its role depends on WHICH key it is; the two sets below cover everything else,
# and `test_the_operand_class_vocabulary_is_covered_exhaustively` fails if a new operand class is
# added without a rule here rather than letting it project no role at all.
_TIME_OPERAND_CLASSES = frozenset({"event_timestamp", "as_of_timestamp"})
# The four non-measure value classes ride with `measure` on the STEP-0 design read, not on a
# guess: `need_metadata._derive_one` resolves every need whose concept has no `entity_link` and a
# `none` `pit_role` — precisely what a dimension / status / direction / policy_input concept is —
# to `JoinRole.MEASURE` (measured over the legacy corpus at 1c656743: 264 of 264 such needs, via
# the `template_default` rung), and `declarations.compile_aggregation` stages exactly the
# `join_role == "measure"` bindings. So this REPRODUCES legacy semantics rather than inventing a
# new one. That an operand nobody intended to aggregate is typed as a measure is G2 — a
# pre-existing semantic SHARED by the legacy and request paths, deliberately left open here and
# settled by the G3 charter; `JoinRole` has exactly five values and none of them says "dimension",
# so the probe has no honest alternative to declare.
_MEASURE_OPERAND_CLASSES = frozenset(
    {"measure", "dimension", "status", "direction", "policy_input"})


def _join_role(raw: str) -> JoinRole | None:
    try:
        return JoinRole(raw) if raw else None
    except ValueError:
        return None


def _temporal_role(raw: str) -> TemporalRole | None:
    try:
        return TemporalRole(raw) if raw else None
    except ValueError:
        return None


def _entity_link(operand: RequiredOperandV1) -> str | None:
    """The entity a key operand NAMES, read off the governed concept registry — the same reading
    `need_metadata` does. A key whose concept links to no entity can never be the anchor: the
    planner refuses a `source_entity_need_role` that does not name an entity-linked need."""
    resolved = concept(operand.concept)
    return resolved.entity_link if resolved is not None else None


def _source_anchor(request: FeaturePlanningRequestV1) -> tuple[str | None, str | None]:
    """The probe's source anchor from the V2 contract's own declarations: the entity_key
    operand COMPATIBLE with the source grain (allowed grains name it, or are unconstrained).
    None/None when absent or ambiguous — the planner's fallback then applies and a genuinely
    ambiguous recipe fails with the planner's NAMED error instead of raising.

    Two DECLARED tie-breaks settle the recipes carrying several compatible keys, applied in this
    order and only while more than one candidate remains:

    1. the key naming the request's own OUTPUT grain — the entity the feature is produced at is
       the anchor even where the contract also requires a relationship to reach it
       (`household_relationship_value` is exactly that shape, so this tie-break must come first);
    2. the key declaring NO `relationship_requirement` — a key the contract says is reachable
       only THROUGH a verified relationship to another entity is a related key, never the source
       row's own anchor (`own_transfer_outflow_amount`'s payee, `first_time_payee_high_value`'s).

    Every one of them is a recipe declaration; nothing here reads a column, a role name or an
    operand's tuple position.
    OUT-OF-MODULE CONSUMER: recipe_operand_policy.population_anchor_and_distinct_roles
    imports this deliberately so the binder and the planner can never disagree about
    who the population is (T8). Reshaping this function must move that caller too.

    """
    candidates = [op for op in request.operands
                  if op.operand_class == "entity_key"
                  and _entity_link(op) is not None
                  and (not op.allowed_source_grains
                       or request.source_grain in op.allowed_source_grains)]
    if len(candidates) > 1:
        at_output_grain = [op for op in candidates
                           if _entity_link(op) == request.output_grain]
        if len(at_output_grain) == 1:
            candidates = at_output_grain
    if len(candidates) > 1:
        unrelated = [op for op in candidates if not op.relationship_requirement]
        if len(unrelated) == 1:
            candidates = unrelated
    if len(candidates) != 1:
        return None, None
    return request.source_grain, candidates[0].role


def _pit_role(operand: RequiredOperandV1) -> str:
    """The concept's governed `pit_role` — the same reading `_derive_one` does, with the same
    fallback for a concept the registry does not resolve."""
    resolved = concept(operand.concept)
    return resolved.pit_role if resolved is not None else "none"


def _derived_roles(request: FeaturePlanningRequestV1, operand: RequiredOperandV1,
                   anchor_role: str | None) -> tuple[JoinRole | None, TemporalRole | None]:
    """The binding roles this operand's DECLARED facts imply — its `operand_class`, the anchor
    `_source_anchor` chose, and the governed concept registry's `entity_link` / `pit_role`. Never
    an operand name, never prose, never tuple position.

    This is CLASS-keyed where `_derive_one`'s ladder is CONCEPT-keyed, so the two agree on 1113 of
    the 1195 V2 operands and disagree on 82 (measured at 1c656743) — every one an operand whose
    authored class and whose concept's governed facts say different things (a `dimension` on an
    entity-linked or pit-bearing concept, and `device_sharing_velocity`'s two). The class is the
    RECIPE AUTHOR's declaration about the slot, so the projection honors it rather than overruling
    it with a role the author did not choose;
    `test_the_class_keyed_projection_diverges_from_the_concept_ladder_only_where_g2_lives` pins the
    divergence by shape so it cannot widen unnoticed, and settling it is G2's ruling."""
    if operand.operand_class == "entity_key":
        if anchor_role is not None and operand.role == anchor_role:
            return JoinRole.SOURCE_ENTITY_KEY, None
        # A key the request's own contract says names the grain it PRODUCES at, versus a key it
        # must pass THROUGH to get there — the plan's roll-up destination versus a hop key.
        #
        # THIS BRANCH IS BRIEF-SPECIFIED AND CORPUS-UNPROVEN. Measured at 1c656743: it fires for
        # 0 of the 1195 V2 operands. Only 4 non-anchor `entity_key` operands exist at all
        # (`own_transfer_outflow_amount.payee` and `first_time_payee_high_value.payee` on
        # `beneficiary`, `customer_worst_days_in_collection.facility` on `facility`,
        # `device_sharing_velocity.device` on a concept linking NO entity) and not one has
        # `entity_link == output_grain`, so every non-anchor key today takes the
        # INTERMEDIATE_ENTITY_KEY line below and the TARGET line has never executed on real data.
        #
        # The comparison is also across TWO UNVALIDATED STRING SPACES: `output_grain` is authored
        # free-text on the recipe, `entity_link` is the concept registry's own vocabulary, and
        # nothing reconciles them. Measured at 1c656743: 40 of the 317 recipes carry an
        # `output_grain` that names NO value in the registry's 40-strong `entity_link` vocabulary
        # — `card` vs `card_account`, `security` vs `instrument`, `reporting_entity` vs
        # `legal_entity`, `debtor` vs `customer`, `device` vs nothing. Those 40 are saved today
        # only by carrying a single (anchor) key operand, so the mismatch never reaches this
        # comparison. That vocabulary gap is NOT this task's to close: it belongs to the G2/G3
        # charter alongside the operand_class-vs-concept divergence above, because reconciling the
        # two spaces is a governance act on the registry, not a projection rule.
        #
        # `test_no_v2_recipe_projects_a_target_entity_key_today` pins the zero, so the FIRST real
        # TARGET projection is a deliberate, visible event that fails a test and forces a ruling —
        # never a silent guess about which of two unreconciled vocabularies was meant.
        if _entity_link(operand) == request.output_grain:
            return JoinRole.TARGET_ENTITY_KEY, None
        return JoinRole.INTERMEDIATE_ENTITY_KEY, None
    if operand.operand_class in _TIME_OPERAND_CLASSES:
        return JoinRole.TIME, _PIT_ROLE_TO_TEMPORAL.get(_pit_role(operand), TemporalRole.NONE)
    if operand.operand_class in _MEASURE_OPERAND_CLASSES:
        return JoinRole.MEASURE, None
    return None, None       # an operand class with no rule — no role, never a guessed one


def _projected_roles(request: FeaturePlanningRequestV1, operand: RequiredOperandV1,
                     anchor_role: str | None) -> tuple[JoinRole | None, TemporalRole | None]:
    """One operand's projected `(join_role, temporal_role)`, per field.

    Declaration precedence is `_derive_one`'s own first rung: a NON-EMPTY declared string wins
    outright over the derivation. A declared string naming no member of the planner's vocabulary
    leaves that field unset — the projection refuses to substitute a derived value for a
    declaration that contradicts it, and it may never mint a role the planner does not have.
    """
    derived_join, derived_temporal = _derived_roles(request, operand, anchor_role)
    return (_join_role(operand.join_role) if operand.join_role else derived_join,
            _temporal_role(operand.temporal_role) if operand.temporal_role else derived_temporal)


def planning_probe(request: FeaturePlanningRequestV1) -> Template:
    """The request's projection into the planner's probe vocabulary — same needs, same bounds,
    identity keyed on the request's own source definition id.

    S1A-4c: each projected need also DECLARES its binding roles. `request_contract` metadata
    resolution deliberately bypasses the legacy resolved-need registry, and 0 of the 1195 operands
    in the V2 registry declare a `join_role` (measured at 1c656743), so before this the planner saw
    needs with no roles at all — and `plan._assemble_rollups` starts a roll-up ONLY from a binding
    whose join role is `source_entity_key`, so no recipe-origin request ever reached its first
    cross-catalog hop.
    """
    source_entity, anchor_role = _source_anchor(request)
    roles = {operand.role: _projected_roles(request, operand, anchor_role)
             for operand in request.operands}
    needs = tuple(
        Need(role=operand.role, concept=operand.concept, optional=not operand.required,
             allowed_source_grains=operand.allowed_source_grains,
             join_role=roles[operand.role][0],
             temporal_role=roles[operand.role][1],
             distinct_binding_group=operand.distinct_binding_group or None,
             alternates=operand.alternative_concepts)
        for operand in request.operands)
    return Template(
        id=request.source_definition_id,
        family=f"planning_request:{request.origin}",
        intent=request.source_definition_id,          # an id, never prose — nothing to leak
        needs=needs,
        params={name: (value,) for name, value in request.parameter_values},
        aggregation=(request.formula.result_class if request.formula is not None
                     else "conceptual"),
        additivity=request.output.additivity,
        explain="M", use_cases=(),
        pit=request.temporal.anchor_kind,
        source_entity=source_entity,
        source_entity_need_role=anchor_role)


def plan_planning_request(conn, *, request: FeaturePlanningRequestV1,
                          target_entity: str | None, scope, roles=(), now,
                          compile_ctx=None, budget=None):
    """One entry for every origin: project, then run the UNCHANGED planner pipeline.

    S1A-2: the probe carries the request's OWN operand metadata, so the planner resolves needs from
    that contract rather than from the legacy `RESOLVED_NEED_METADATA` registry — 106 legacy
    template ids collide with V2 recipe ids, and a colliding id would otherwise override the
    request's declarations with a same-named legacy template's (37 of 317 recipes measurably).
    """
    from featuregen.overlay.upload.planner.plan import plan_bindings

    return plan_bindings(conn, template=planning_probe(request),
                         target_entity=target_entity, scope=scope, roles=roles, now=now,
                         compile_ctx=compile_ctx, budget=budget,
                         metadata_resolution_mode="request_contract")


__all__ = ["plan_planning_request", "planning_probe"]
