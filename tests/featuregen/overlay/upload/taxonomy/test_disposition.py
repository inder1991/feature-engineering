"""Phase-1B Task 5 — the per-stage disposition model.

Exercises ``evaluate_dispositions``: it consumes the SAME :class:`ApplicabilityResult` grounding
already ran on (no recompute) plus the grounding/gauntlet outcomes (``grounded_ids`` = recipes that
bound a surviving candidate; ``rejected`` = recipes the safety gauntlet refused) and yields one
:class:`RecipeEvaluation` per recipe with three stamped, versioned stage evaluations.

The behaviours pinned here are the plan's Global Constraints: an ``out_of_scope`` recipe leaves its
downstream stages ``NOT_EVALUATED`` (never a bare ``None``); each stage carries ``evaluation_version``
+ ``evaluated_at`` for replay; there is exactly one evaluation per recipe in ``by_recipe``; and an
``unscoped`` result puts no recipe out of scope. Ranking is a Phase-2 *attribute*, not a disposition.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.upload.taxonomy.applicability import (
    ApplicabilityResult,
    ConfirmedScope,
    applicability_result,
)
from featuregen.overlay.upload.taxonomy.disposition import (
    FinalDisposition,
    RecipeEvaluation,
    StageStatus,
    evaluate_dispositions,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES

CHURN = "customer.relationship_attrition.churn"
NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)
VERSION = "1.0.0"
ALL_IDS = {t.id for t in ALL_TEMPLATES}

# A credit and a fraud recipe — both out of scope for a bare churn scope (see test_applicability).
CREDIT_RECIPE = "credit_utilisation"
FRAUD_RECIPE = "txn_velocity_spike"
# Churn-family recipes (primary for a confirmed churn objective).
CHURN_ELIGIBLE = "balance_trend"
CHURN_REJECTED = "dormancy_days"
CHURN_UNBUILDABLE = "product_breadth"


def _churn_result() -> ApplicabilityResult:
    return applicability_result(ConfirmedScope(primary=CHURN))


def _by_id(evals: list[RecipeEvaluation]) -> dict[str, RecipeEvaluation]:
    return {ev.recipe_id: ev for ev in evals}


def test_out_of_scope_recipe_leaves_downstream_not_evaluated() -> None:
    result = _churn_result()
    assert result.by_recipe[CREDIT_RECIPE] == "out_of_scope"
    evals = _by_id(evaluate_dispositions(
        result, grounded_ids=frozenset(), rejected={}, evaluation_version=VERSION, now=NOW))

    ev = evals[CREDIT_RECIPE]
    # applicability ran and completed, carrying the out-of-scope reason from the shared result.
    assert ev.applicability.status is StageStatus.COMPLETED
    assert ev.applicability.reason_codes == result.reason_codes[CREDIT_RECIPE]
    assert ev.applicability.reason_codes == ("no_confirmed_use_case_match",)
    # Downstream stages are NOT_EVALUATED — never a bare null.
    assert ev.grounding.status is StageStatus.NOT_EVALUATED
    assert ev.grounding.status is not None
    assert ev.safety.status is StageStatus.NOT_EVALUATED
    assert ev.grounding.reason_codes == ("prior_stage_out_of_scope",)
    assert ev.safety.reason_codes == ("prior_stage_out_of_scope",)
    assert ev.final_disposition is FinalDisposition.OUT_OF_SCOPE
    assert ev.relevance_tier is None


def test_in_scope_grounded_recipe_is_eligible() -> None:
    result = _churn_result()
    assert result.by_recipe[CHURN_ELIGIBLE] == "primary"
    evals = _by_id(evaluate_dispositions(
        result, grounded_ids=frozenset({CHURN_ELIGIBLE}), rejected={},
        evaluation_version=VERSION, now=NOW))

    ev = evals[CHURN_ELIGIBLE]
    assert ev.applicability.status is StageStatus.COMPLETED
    assert ev.grounding.status is StageStatus.COMPLETED
    assert ev.safety.status is StageStatus.COMPLETED
    assert ev.final_disposition is FinalDisposition.ELIGIBLE
    assert ev.relevance_tier == "primary"


def test_in_scope_rejected_recipe_is_safety_rejected() -> None:
    result = _churn_result()
    reason = ("pii_leakage", "unsafe_join")
    evals = _by_id(evaluate_dispositions(
        result, grounded_ids=frozenset(), rejected={CHURN_REJECTED: reason},
        evaluation_version=VERSION, now=NOW))

    ev = evals[CHURN_REJECTED]
    # It bound (grounding completed) but the gauntlet refused it (safety failed with the reason).
    assert ev.grounding.status is StageStatus.COMPLETED
    assert ev.safety.status is StageStatus.FAILED
    assert ev.safety.reason_codes == reason
    assert ev.final_disposition is FinalDisposition.SAFETY_REJECTED
    assert ev.relevance_tier == "primary"


def test_in_scope_ungrounded_recipe_is_unbuildable() -> None:
    result = _churn_result()
    assert result.by_recipe[CHURN_UNBUILDABLE] == "primary"
    evals = _by_id(evaluate_dispositions(
        result, grounded_ids=frozenset(), rejected={}, evaluation_version=VERSION, now=NOW))

    ev = evals[CHURN_UNBUILDABLE]
    # Grounding ran but nothing bound (no candidate) -> unbuildable; safety has nothing to check.
    assert ev.grounding.status is StageStatus.COMPLETED
    assert ev.grounding.reason_codes == ("no_binding",)
    assert ev.safety.status is StageStatus.NOT_EVALUATED
    assert ev.safety.reason_codes == ("no_binding",)
    assert ev.final_disposition is FinalDisposition.UNBUILDABLE
    assert ev.relevance_tier == "primary"


def test_every_stage_is_versioned_and_timestamped_on_all_recipes() -> None:
    result = _churn_result()
    evals = evaluate_dispositions(
        result,
        grounded_ids=frozenset({CHURN_ELIGIBLE}),
        rejected={CHURN_REJECTED: ("pii_leakage",)},
        evaluation_version=VERSION,
        now=NOW,
    )
    for ev in evals:
        for stage in (ev.applicability, ev.grounding, ev.safety):
            assert stage.evaluation_version == VERSION
            assert stage.evaluated_at is NOW


def test_exactly_one_evaluation_per_recipe_in_by_recipe() -> None:
    result = _churn_result()
    evals = evaluate_dispositions(
        result, grounded_ids=frozenset(), rejected={}, evaluation_version=VERSION, now=NOW)
    ids = [ev.recipe_id for ev in evals]
    # One evaluation per recipe in the shared applicability result — count and key-set both match.
    assert len(evals) == len(result.by_recipe)
    assert len(ids) == len(set(ids))
    assert set(ids) == set(result.by_recipe)


def test_unscoped_result_puts_no_recipe_out_of_scope() -> None:
    result = applicability_result(ConfirmedScope(primary=None, unscoped=True))
    evals = evaluate_dispositions(
        result, grounded_ids=frozenset(), rejected={}, evaluation_version=VERSION, now=NOW)
    assert all(ev.final_disposition is not FinalDisposition.OUT_OF_SCOPE for ev in evals)
    assert all(ev.relevance_tier == "primary" for ev in evals)
