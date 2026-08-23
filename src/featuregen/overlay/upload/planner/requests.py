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
`temporal_role` translate only where the request's string names a planner vocabulary member
(anything else stays None — DERIVED by the planner's own concept metadata, never guessed);
parameters flatten to their RESOLVED values (a request is one variant by contract).

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
from featuregen.overlay.upload.templates import Need, Template


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
    operand's tuple position."""
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


def planning_probe(request: FeaturePlanningRequestV1) -> Template:
    """The request's projection into the planner's probe vocabulary — same needs, same bounds,
    identity keyed on the request's own source definition id."""
    source_entity, anchor_role = _source_anchor(request)
    needs = tuple(
        Need(role=operand.role, concept=operand.concept, optional=not operand.required,
             allowed_source_grains=operand.allowed_source_grains,
             join_role=_join_role(operand.join_role),
             temporal_role=_temporal_role(operand.temporal_role),
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
