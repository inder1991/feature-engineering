"""Phase-1A Task 3 — the applicability evaluator (recognised scope -> in-scope recipe ids).

Exercises ``scope_from_recognition`` (a recognition result -> a ``ConfirmedScope``) and
``in_scope_recipes`` (a confirmed scope -> the ``(primary_scoped, supporting_scoped)`` recipe-id
sets). The behaviours pinned here are the Phase-1B contract: EXACT never expands a bare parent,
INCLUDE_DESCENDANTS pulls in a confirmed parent's leaf recipes, supporting (secondary-match) is never
capped, and an ``unscoped`` scope fails open to every recipe. See
``docs/superpowers/plans/2026-07-09-phase1a-shadow-recognizer.md`` Task 3.
"""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.applicability import (
    ApplicabilityResult,
    ConfirmedScope,
    ScopeExpansion,
    applicability_result,
    in_scope_recipes,
    scope_from_recognition,
)
from featuregen.overlay.upload.taxonomy.recognition import (
    TAXONOMY_VERSION,
    RecognitionResult,
    RecognitionStatus,
    UseCaseCandidate,
    unscoped_result,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES

CHURN = "customer.relationship_attrition.churn"
PRIMACY = "customer.relationship_attrition.primacy_loss"
ALL_IDS = {t.id for t in ALL_TEMPLATES}


def test_exact_churn_scopes_churn_recipes_and_no_credit_or_fraud() -> None:
    primary, supporting = in_scope_recipes(ConfirmedScope(primary=CHURN))
    # The churn-family recipes ground.
    assert "balance_trend" in primary
    assert "dormancy_days" in primary
    # A bare EXACT churn scope never reaches into credit or fraud.
    assert "credit_utilisation" not in primary | supporting
    assert "txn_velocity_spike" not in primary | supporting


def test_unscoped_fails_open_to_all_recipes() -> None:
    primary, supporting = in_scope_recipes(ConfirmedScope(primary=None, unscoped=True))
    assert primary == ALL_IDS
    assert len(primary) == 153
    assert supporting == set()


def test_include_descendants_pulls_in_credit_leaf_recipes() -> None:
    # A confirmed BARE PARENT ("credit") under INCLUDE_DESCENDANTS grounds its leaf recipes...
    primary, _supporting = in_scope_recipes(
        ConfirmedScope(primary="credit", expansion=ScopeExpansion.INCLUDE_DESCENDANTS))
    # credit_utilisation's applicability primary is the credit.early_warning leaf (a descendant of credit).
    assert "credit_utilisation" in primary


def test_exact_bare_parent_matches_nothing_directly() -> None:
    # ...but the SAME bare-parent scope under EXACT matches nothing (no recipe's primary IS "credit").
    primary, _supporting = in_scope_recipes(ConfirmedScope(primary="credit"))
    assert "credit_utilisation" not in primary
    assert primary == set()


def test_secondary_match_is_supporting_not_primary() -> None:
    # external_own_transfer_trend carries primacy_loss as a SECONDARY (its own primary is churn).
    primary, supporting = in_scope_recipes(ConfirmedScope(primary=PRIMACY))
    assert "external_own_transfer_trend" in supporting
    assert "external_own_transfer_trend" not in primary


def test_scope_from_recognition_unscoped_result() -> None:
    result = unscoped_result("nothing clearly applies", model_id="claude-opus-4-8", prompt_version="1")
    scope = scope_from_recognition(result)
    assert scope.unscoped is True
    assert scope.primary is None
    assert scope.secondary == ()
    assert scope.expansion is ScopeExpansion.EXACT


def test_scope_from_recognition_technical_failure_is_unscoped() -> None:
    result = unscoped_result(
        "provider refusal", model_id="claude-opus-4-8", prompt_version="1", technical=True)
    assert result.status is RecognitionStatus.TECHNICAL_FAILURE
    assert scope_from_recognition(result).unscoped is True


def test_scope_from_recognition_classified_maps_primary_and_secondary() -> None:
    result = RecognitionResult(
        status=RecognitionStatus.CLASSIFIED,
        candidates=(
            UseCaseCandidate(
                use_case_id=CHURN, relationship="primary", confidence="high",
                evidence_spans=("close their account",), rationale="clear churn intent"),
            UseCaseCandidate(
                use_case_id=PRIMACY, relationship="secondary", confidence="medium",
                evidence_spans=("salary redirected",), rationale="primacy erosion"),
        ),
        ambiguity_note=None,
        taxonomy_version=TAXONOMY_VERSION,
        recognizer_model_id="claude-opus-4-8",
        prompt_version="1",
    )
    scope = scope_from_recognition(result)
    assert scope.unscoped is False
    assert scope.primary == CHURN
    assert scope.secondary == (PRIMACY,)
    assert scope.expansion is ScopeExpansion.EXACT
    # And the confirmed churn primary grounds the churn recipes.
    primary, _supporting = in_scope_recipes(scope)
    assert "balance_trend" in primary


# ── review fix: fail-open on a primary-less / empty scope (never scope to zero recipes) ──────────────
def test_ambiguous_result_without_primary_fails_open():
    # An AMBIGUOUS result carrying only secondary alternatives has no confident primary -> unscoped.
    ambiguous = RecognitionResult(
        status=RecognitionStatus.AMBIGUOUS,
        candidates=(UseCaseCandidate(
            use_case_id="credit.early_warning", relationship="secondary",
            confidence="low", evidence_spans=("x",), rationale="r"),),
        ambiguity_note="spans two objectives", taxonomy_version=TAXONOMY_VERSION,
        recognizer_model_id="m", prompt_version="1")
    scope = scope_from_recognition(ambiguous)
    assert scope.unscoped is True and scope.primary is None
    primary_scoped, supporting = in_scope_recipes(scope)
    assert primary_scoped == ALL_IDS and supporting == set()


def test_empty_scope_fails_open_to_all():
    # Defense-in-depth: a hand-built primary-less, secondary-less, non-unscoped scope still grounds all.
    primary_scoped, supporting = in_scope_recipes(ConfirmedScope(primary=None, secondary=()))
    assert primary_scoped == ALL_IDS and supporting == set()


# ── Phase-1B Task 3: applicability_result — exactly one decision per recipe ──────────────────────────
def test_applicability_result_classifies_every_recipe_exactly_once() -> None:
    result = applicability_result(ConfirmedScope(primary=CHURN))
    assert isinstance(result, ApplicabilityResult)
    # EVERY recipe appears exactly once (dict keys are unique, so equality of key-set == exactly-once).
    assert set(result.by_recipe) == ALL_IDS
    assert len(result.by_recipe) == 153
    # The confirmed churn objective's recipes are primary...
    assert result.by_recipe["balance_trend"] == "primary"
    assert result.by_recipe["dormancy_days"] == "primary"
    # ...and a credit recipe and a fraud recipe fall out of scope.
    assert result.by_recipe["credit_utilisation"] == "out_of_scope"
    assert result.by_recipe["txn_velocity_spike"] == "out_of_scope"
    # Every classification is one of the three allowed relationships.
    assert set(result.by_recipe.values()) <= {"primary", "supporting", "out_of_scope"}


def test_applicability_result_eligible_ids_match_in_scope_recipes() -> None:
    scope = ConfirmedScope(primary=CHURN)
    primary_scoped, supporting_scoped = in_scope_recipes(scope)
    result = applicability_result(scope)
    assert result.eligible_ids == frozenset(primary_scoped | supporting_scoped)
    # And eligible_ids is exactly the non-out_of_scope recipes.
    assert result.eligible_ids == frozenset(
        rid for rid, rel in result.by_recipe.items() if rel != "out_of_scope")


def test_applicability_result_unscoped_is_all_primary() -> None:
    result = applicability_result(ConfirmedScope(primary=None, unscoped=True))
    assert set(result.by_recipe.values()) == {"primary"}
    assert all(rel == "primary" for rel in result.by_recipe.values())
    assert "out_of_scope" not in result.by_recipe.values()
    assert result.eligible_ids == frozenset(ALL_IDS)
    assert len(result.eligible_ids) == 153


def test_applicability_result_secondary_match_is_supporting() -> None:
    # external_own_transfer_trend carries primacy_loss as a SECONDARY (its own primary is churn).
    result = applicability_result(ConfirmedScope(primary=PRIMACY))
    assert result.by_recipe["external_own_transfer_trend"] == "supporting"
    # Its reason code reflects the secondary (supporting) match.
    assert result.reason_codes["external_own_transfer_trend"] == ("secondary_match",)


def test_applicability_result_reason_codes_cover_every_recipe() -> None:
    result = applicability_result(ConfirmedScope(primary=CHURN))
    assert set(result.reason_codes) == ALL_IDS
    assert result.reason_codes["balance_trend"] == ("primary_match",)
    assert result.reason_codes["credit_utilisation"] == ("no_confirmed_use_case_match",)
