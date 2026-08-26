"""A6 — the G2 SERVING GATE: is this operand's governed join role actually resolved?

The platform holds TWO governed authorities on what an operand contributes to a join, and they
are not the same reading:

* the **recipe author's declaration** — ``RequiredOperandV1.operand_class``, projected onto the
  planner's ``JoinRole`` vocabulary by ``planner/requests._derived_roles`` (class-keyed);
* the **concept registry's ladder** — ``need_metadata._derive_one``: ``entity_link`` first, then
  ``pit_role``, then ``JoinRole.MEASURE`` as the template default (concept-keyed).

They agree on 1113 of the V2 registry's 1195 operands and DISAGREE on 82 (§V fact V2, measured at
``1c656743``). That divergence is G2. It is invisible today because ``compile_aggregation``
short-circuits on ``card is None`` (``declarations.py``) before the additivity matrix runs, so an
operand nobody intended to aggregate is staged as a measure and the mis-classification only
surfaces when a cardinality attaches.

**This module does NOT settle G2** — the ruling is chartered. It makes the divergence a NAMED
serving fact: :data:`OPERAND_ROLE_UNRESOLVED`, whose registered disposition BLOCKS all six
actions while leaving the card visible as setup work. The narrow "correctly classify a dimension"
fix is deliberately not taken here; taking it would decide, per operand, a question with an owner.

**Derived, never listed.** The check re-asks the same two authorities the 82-operand worklist
counted — ``planning_probe`` (which applies ``_projected_roles``, i.e. declaration-first then the
class-keyed derivation) against ``derive_need_metadata`` over the SAME probe with its roles
stripped. Nothing here enumerates recipes, operands or concepts, so the gate moves with the
registries rather than rotting into a stale inventory. The worklist test
(``planner/test_requests.py``) pins the divergence BY SHAPE; A6's own test recomputes the
criterion independently over all 317 recipes and demands set equality with this module's answer.

**Three ways an operand is unresolved**, and they are one product fact with three details:

1. the two authorities DISAGREE (G2 proper);
2. the operand's class yields no rule at all, or its DECLARED ``join_role`` names no member of
   the planner's five-value vocabulary — either way the planner carries no role for the slot;
3. the concept ladder REFUSES the request's shape outright (an ambiguous source anchor). That is
   fail-CLOSED: every operand of such a request is unresolved, because roles could not be
   resolved at all.

**Resolved by DECLARATION.** A non-empty, valid ``RequiredOperandV1.join_role`` wins outright —
that is the platform's own first rung (``_projected_roles``), and it is exactly the shape G2's
ruling will take, one operand at a time. So the gate has a cure that needs no new mechanism.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
)
from featuregen.overlay.upload.need_metadata import (
    derive_need_metadata,
    validate_template_anchor,
)
from featuregen.overlay.upload.planner.requests import planning_probe

# ONE spelling, imported from the vocabulary that owns it — the precedent
# `semantic_eligibility_reasons` states in its own comment and that `planner/physical_plan_v1`
# follows for `ALLOCATION_POLICY_REQUIRED`. A module-local literal is a second spelling waiting
# to drift out of the disposition table.
from featuregen.overlay.upload.semantic_eligibility_reasons import OPERAND_ROLE_UNRESOLVED

__all__ = [
    "OPERAND_ROLE_UNRESOLVED",
    "OperandRoleResolutionV1",
    "operand_role_resolutions",
    "unresolved_operand_roles",
]


@dataclass(frozen=True, slots=True)
class OperandRoleResolutionV1:
    """One operand's governed join role, and what each authority said about it.

    Both authorities' answers are carried even when they agree: "the two agreed on MEASURE" and
    "the author declared MEASURE and nobody derived anything" are different facts, and a consumer
    that can only see the verdict cannot tell an operator which one it is looking at."""

    role: str
    concept: str
    operand_class: str
    #: the operand's own declaration, verbatim ("" when it declared nothing)
    declared_join_role: str
    #: the role the PLANNER will actually carry for this slot (declaration-first, then the
    #: class-keyed derivation) — ``None`` when neither yields one
    projected_join_role: JoinRole | None
    #: what ``need_metadata._derive_one`` derives from the concept registry alone
    concept_ladder_join_role: JoinRole | None
    resolved: bool
    detail: str


def _resolution(operand: RequiredOperandV1, projected: JoinRole | None,
                ladder: JoinRole | None) -> OperandRoleResolutionV1:
    declared = operand.join_role or ""

    def built(resolved: bool, detail: str) -> OperandRoleResolutionV1:
        return OperandRoleResolutionV1(
            role=operand.role, concept=operand.concept, operand_class=operand.operand_class,
            declared_join_role=declared, projected_join_role=projected,
            concept_ladder_join_role=ladder, resolved=resolved, detail=detail)

    if projected is None:
        if declared:
            return built(False, (
                f"operand {operand.role!r} declares join_role {declared!r}, which names no member "
                f"of the planner's JoinRole vocabulary; the planner carries no role for this slot "
                f"and the declaration is not silently replaced by a derived one"))
        return built(False, (
            f"operand {operand.role!r} has operand_class {operand.operand_class!r}, for which the "
            f"projection carries no rule, and it declares no join_role; the planner carries no "
            f"role for this slot"))
    if declared:
        return built(True, (
            f"operand {operand.role!r} DECLARES join_role {projected.value!r}; a declaration is "
            f"the platform's first rung and settles the slot"))
    if ladder is projected:
        return built(True, (
            f"both governed authorities resolve operand {operand.role!r} to "
            f"{projected.value!r}"))
    return built(False, (
        f"the two governed authorities disagree about operand {operand.role!r}: its "
        f"operand_class {operand.operand_class!r} projects {projected.value!r} while the concept "
        f"registry's ladder derives {ladder.value if ladder else None!r} from concept "
        f"{operand.concept!r}. The platform cannot state what this operand contributes, so it is "
        f"not served until the role is ruled on (a declared join_role settles it)"))


def _all_unresolved(request: FeaturePlanningRequestV1, detail: str,
                    ) -> tuple[OperandRoleResolutionV1, ...]:
    return tuple(
        OperandRoleResolutionV1(
            role=operand.role, concept=operand.concept, operand_class=operand.operand_class,
            declared_join_role=operand.join_role or "", projected_join_role=None,
            concept_ladder_join_role=None, resolved=False, detail=detail)
        for operand in request.operands)


def operand_role_resolutions(
    request: FeaturePlanningRequestV1,
) -> tuple[OperandRoleResolutionV1, ...]:
    """Every operand of ``request``, with both governed authorities' answers. Pure; reads no
    database — a role is a governed DECLARATION question, never a physical one."""
    probe = planning_probe(request)
    projected = {need.role: need.join_role for need in probe.needs}
    stripped = dataclasses.replace(probe, needs=tuple(
        dataclasses.replace(need, join_role=None, temporal_role=None) for need in probe.needs))
    # ▲ ONLY the anchor validator is guarded, and it is called explicitly rather than reached
    # through `derive_need_metadata`. A blanket `except ValueError` around the whole derivation
    # would report "the source anchor is ambiguous" for any ValueError raised anywhere beneath
    # it — a diagnosis the gate would not have earned. Anything else propagates.
    try:
        validate_template_anchor(stripped)
    except ValueError as exc:
        # The ladder cannot answer for this request at all, so no operand of it has a settled
        # role. Fail CLOSED: never swallowed into "resolved".
        return _all_unresolved(request, (
            f"the concept registry's ladder cannot resolve roles for this request at all — its "
            f"source anchor is ambiguous ({exc}); every operand is unresolved until the request "
            f"names one"))
    ladder = {meta.role: meta.join_role for meta in derive_need_metadata(stripped)}
    return tuple(
        _resolution(operand, projected.get(operand.role), ladder.get(operand.role))
        for operand in request.operands)


def unresolved_operand_roles(
    request: FeaturePlanningRequestV1, *, roles: frozenset[str] | None = None,
) -> tuple[OperandRoleResolutionV1, ...]:
    """The operands whose governed join role is NOT resolved — the gate's facts.

    ``roles``, when given, narrows the answer to the operands a served option actually BINDS: an
    operand the plan never bound is not part of that option's computation, so gating on it would
    refuse a card for a slot nobody is reading."""
    return tuple(
        resolution for resolution in operand_role_resolutions(request)
        if not resolution.resolved and (roles is None or resolution.role in roles))
