"""BR-23 proper — the validity fold: required roles from declarations, gaps always named."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.recipe_review import RecipeReviewEventV1
from featuregen.overlay.upload.recipe_review_validity import (
    required_reviewer_roles,
    review_coverage_report,
    review_validity,
)


def _event(role: str, *, decision: str = "approved",
           reviewer: str = "user:a") -> RecipeReviewEventV1:
    return RecipeReviewEventV1(
        event_id=f"rre_{role}", recipe_id="r", recipe_revision_hash="h",
        output_id="o", decision=decision, reviewer=reviewer, reviewer_role=role,
        reviewed_primary_objective="", reviewed_supporting_objectives=(),
        formula_expectation_hash=None, gold_corpus_refs=(), policy_dependencies=(),
        permitted_stages=(), prohibited_stages=(), rationale="",
        evidence_refs=(), supersedes_event_id=None)


def test_required_roles_derive_from_the_recipes_own_declarations():
    """SME + semantic owner always; engineering for executables; model risk for near-label/
    outcome/model outputs; privacy for privacy_purpose policies; the regulatory specialist
    for regulatory classifications — each read off a REAL registry recipe."""
    plain = required_reviewer_roles(v2_recipe_by_id("posted_debit_amount"))
    assert plain == ("banking_sme", "data_semantic_owner", "formula_engineering")

    near_label = required_reviewer_roles(v2_recipe_by_id("days_past_due_max"))
    assert "model_risk" in near_label

    model_output = required_reviewer_roles(v2_recipe_by_id("churn_probability"))
    assert "model_risk" in model_output
    assert "formula_engineering" not in model_output

    privacy = required_reviewer_roles(v2_recipe_by_id("vulnerability_indicator_flag"))
    assert "privacy_compliance" in privacy

    regulatory = required_reviewer_roles(
        v2_recipe_by_id("liability_cash_outflow_contribution"))
    assert "treasury_regulatory_accounting" in regulatory


def test_validity_names_every_gap():
    recipe = v2_recipe_by_id("posted_debit_amount")
    v = review_validity(recipe, {})
    assert not v.current
    assert v.missing_roles == v.required_roles

    partial = {"banking_sme": _event("banking_sme")}
    v = review_validity(recipe, partial)
    assert not v.current
    assert set(v.missing_roles) == {"data_semantic_owner", "formula_engineering"}

    blocked = {**partial,
               "data_semantic_owner": _event("data_semantic_owner",
                                             decision="changes_required")}
    v = review_validity(recipe, blocked)
    assert "data_semantic_owner:changes_required" in v.blocking_decisions
    assert "data_semantic_owner" not in v.missing_roles


def test_a_single_identity_cannot_approve_every_required_role():
    recipe = v2_recipe_by_id("posted_debit_amount")
    all_one_person = {role: _event(role, reviewer="user:solo")
                      for role in required_reviewer_roles(recipe)}
    v = review_validity(recipe, all_one_person)
    assert v.single_identity_violation and not v.current

    two_people = dict(all_one_person)
    two_people["formula_engineering"] = _event("formula_engineering",
                                               reviewer="user:other")
    v = review_validity(recipe, two_people)
    assert v.current and not v.single_identity_violation


def test_a_conceptual_recipe_is_approvable_as_an_idea_without_becoming_executable():
    """The acceptance: SME approval of an idea needs no engineering signature and cannot
    touch readiness — the fold never emits readiness at all."""
    idea = v2_recipe_by_id("share_of_wallet")
    roles = required_reviewer_roles(idea)
    assert "formula_engineering" not in roles
    approvals = {}
    reviewers = iter(("user:a", "user:b", "user:c", "user:d"))
    for role in roles:
        approvals[role] = _event(role, reviewer=next(reviewers))
    v = review_validity(idea, approvals)
    assert v.current
    assert idea.readiness == "CONCEPTUAL_ONLY"          # untouched, structurally


def test_the_batch_report_counts_current_reviews_by_family_and_readiness():
    recipes = [v2_recipe_by_id("posted_debit_amount"),
               v2_recipe_by_id("posted_credit_amount")]
    exemplar = recipes[0]
    approvals = {}
    reviewers = iter(("user:a", "user:b", "user:c", "user:d"))
    for role in required_reviewer_roles(exemplar):
        approvals[role] = _event(role, reviewer=next(reviewers))
    validity = {exemplar.recipe_id: review_validity(exemplar, approvals)}
    report = review_coverage_report(recipes, validity)
    row = report["transaction_foundation"]
    assert row["FORMULA_AUTHORABLE"] == {"recipes": 1, "review_current": 1}
    assert row["FORMULA_BLOCKED"] == {"recipes": 1, "review_current": 0}
