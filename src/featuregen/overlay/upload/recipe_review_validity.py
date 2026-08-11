"""BR-23 — the review-validity fold: which approvals a recipe NEEDS, and whether it has them.

Pure and total, like every governance fold in this program: the recipe's own declarations
derive its REQUIRED reviewer roles, the caller supplies the current (revision-specific)
approvals, and the fold answers with named gaps — never a boolean shrug.

The role law, from the plan's review-roles list:

* ``banking_sme`` and ``data_semantic_owner`` — always;
* ``formula_engineering`` — for anything executable (deterministic formulas);
* ``model_risk`` — when the recipe is a governed model output OR its leakage classification
  is near_label/outcome (the plan: near-label and model-output recipes require the risk view);
* ``privacy_compliance`` — when any ``privacy_purpose:`` policy rides the recipe (operands or
  eligibility): sensitive, suitability, consent and protected-purpose controls;
* ``treasury_regulatory_accounting`` — when the output depends on a regulatory classification
  (any ``risk_corridor:`` policy) or a ``model_output:`` accounting-engine provenance.

Two rules the fold enforces beyond role coverage: authors cannot SELF-approve every required
role (multi-role recipes need at least two distinct reviewer identities), and a CONCEPTUAL
recipe can be SME-approved AS AN IDEA — validity for it requires only the SME and semantic
owner, and approval never makes it executable (readiness stays CONCEPTUAL_ONLY structurally;
this fold cannot touch readiness at all).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.overlay.upload.recipe_review import RecipeReviewEventV1


def _policy_refs(recipe) -> tuple[str, ...]:
    refs = list(recipe.eligibility.policy_refs)
    for operand in recipe.operands:
        if operand.status_policy_ref:
            refs.append(operand.status_policy_ref)
    for parameter in recipe.parameters:
        if parameter.governed_policy_ref:
            refs.append(parameter.governed_policy_ref)
    return tuple(refs)


def required_reviewer_roles(recipe) -> tuple[str, ...]:
    """The recipe's OWN declarations decide who must sign — nothing here is optional prose."""
    roles = ["banking_sme", "data_semantic_owner"]
    if recipe.computation_kind == "deterministic_formula":
        roles.append("formula_engineering")
    if (recipe.computation_kind == "governed_model_output"
            or recipe.leakage.classification in ("near_label", "outcome")):
        roles.append("model_risk")
    refs = _policy_refs(recipe)
    if any(ref.startswith("privacy_purpose:") for ref in refs):
        roles.append("privacy_compliance")
    if any(ref.startswith(("risk_corridor:", "model_output:")) for ref in refs):
        roles.append("treasury_regulatory_accounting")
    return tuple(roles)


@dataclass(frozen=True, slots=True)
class ReviewValidityV1:
    """The fold's answer: current or not, with every gap NAMED."""

    current: bool                              # every required role approved at THIS revision
    required_roles: tuple[str, ...]
    approved_roles: tuple[str, ...]
    missing_roles: tuple[str, ...]
    blocking_decisions: tuple[str, ...]        # roles whose newest decision blocks (not approved)
    single_identity_violation: bool            # one person signed a multi-role requirement


def review_validity(recipe,
                    reviews_by_role: Mapping[str, RecipeReviewEventV1]) -> ReviewValidityV1:
    """Fold the recipe's requirements against its revision-specific reviews.

    ``reviews_by_role`` must already be REVISION-SPECIFIC (built from ``current_review`` at the
    recipe's canonical hash — an edited definition supplies an empty mapping, which is exactly
    how a changed formula stales every approval with no flag to forget)."""
    required = required_reviewer_roles(recipe)
    approved, blocking = [], []
    for role in required:
        event = reviews_by_role.get(role)
        if event is None:
            continue
        if event.decision == "approved":
            approved.append(role)
        else:
            blocking.append(f"{role}:{event.decision}")
    missing = tuple(role for role in required
                    if role not in approved
                    and not any(b.startswith(f"{role}:") for b in blocking))

    approvers = {reviews_by_role[role].reviewer for role in approved}
    single_identity = len(required) > 1 and len(approved) == len(required) \
        and len(approvers) < 2

    current = (not missing and not blocking and not single_identity
               and len(approved) == len(required))
    return ReviewValidityV1(
        current=current,
        required_roles=required,
        approved_roles=tuple(approved),
        missing_roles=missing,
        blocking_decisions=tuple(blocking),
        single_identity_violation=single_identity)


def reviews_by_role_for_revision(conn, *, recipe_id: str,
                                 recipe_revision_hash: str,
                                 ) -> dict[str, RecipeReviewEventV1]:
    """The newest event PER ROLE at this exact revision — the fold's input, from the store."""
    from featuregen.overlay.upload.recipe_review import review_events

    by_role: dict[str, RecipeReviewEventV1] = {}
    for event in review_events(conn, recipe_id):        # oldest first; later wins per role
        if event.recipe_revision_hash == recipe_revision_hash:
            by_role[event.reviewer_role] = event
    return by_role


def review_coverage_report(recipes, validity_by_recipe: Mapping[str, ReviewValidityV1]) -> dict:
    """The batch report: per family × readiness, how many recipes hold CURRENT approval —
    the number BR-24's activation gate reads."""
    report: dict[str, dict] = {}
    for recipe in recipes:
        bucket = report.setdefault(recipe.family, {})
        row = bucket.setdefault(recipe.readiness, {"recipes": 0, "review_current": 0})
        row["recipes"] += 1
        validity = validity_by_recipe.get(recipe.recipe_id)
        if validity is not None and validity.current:
            row["review_current"] += 1
    return report
