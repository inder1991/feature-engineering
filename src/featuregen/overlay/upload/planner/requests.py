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
from featuregen.overlay.upload.feature_planning_contracts import FeaturePlanningRequestV1
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


def planning_probe(request: FeaturePlanningRequestV1) -> Template:
    """The request's projection into the planner's probe vocabulary — same needs, same bounds,
    identity keyed on the request's own source definition id."""
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
        pit=request.temporal.anchor_kind)


def plan_planning_request(conn, *, request: FeaturePlanningRequestV1,
                          target_entity: str | None, scope, roles=(), now,
                          compile_ctx=None, budget=None):
    """One entry for every origin: project, then run the UNCHANGED planner pipeline."""
    from featuregen.overlay.upload.planner.plan import plan_bindings

    return plan_bindings(conn, template=planning_probe(request),
                         target_entity=target_entity, scope=scope, roles=roles, now=now,
                         compile_ctx=compile_ctx, budget=budget)


__all__ = ["plan_planning_request", "planning_probe"]
