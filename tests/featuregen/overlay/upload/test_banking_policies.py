"""BR-10 — the governed policy-kind registry: closed, declaration-bearing, and wired to BR-6/BR-7.

The registry names the kinds; it creates no second store. What these tests pin: the eleven
plan-named kinds exist with non-vacuous declaration schemas; every BR-6 authority-ref field maps
to a kind (total by introspection, so a new ref field cannot ship unmapped); refs and
declarations REFUSE rather than approximate; and the acceptance sentence — a filtered formula
whose source never declared its status policy is NOT materialization-ready — holds through
BR-7's real fold.
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from featuregen.formula.schema_v2 import AuthorityRefsV2
from featuregen.overlay.upload.banking_policies import (
    AUTHORITY_REF_KINDS,
    BANKING_POLICY_KINDS,
    POLICY_KIND_REGISTRY,
    PolicyKindError,
    parse_policy_ref,
    policy_blockers,
    required_policy_kinds,
    validate_policy_declaration,
)
from featuregen.overlay.upload.recipe_readiness import ReadinessInputsV1, fold_readiness

PLAN_KINDS = ("eligible_status", "direction_sign", "reversal_correction", "active_state",
              "currency_conversion", "business_calendar", "allocation", "threshold",
              "risk_corridor", "model_output", "privacy_purpose")


def test_the_eleven_plan_kinds_exist_closed_and_declaration_bearing():
    assert tuple(k.kind for k in BANKING_POLICY_KINDS) == PLAN_KINDS
    for kind in BANKING_POLICY_KINDS:
        assert kind.required_declarations, kind.kind
        assert kind.resolution_home, kind.kind


def test_a_threshold_is_not_a_number():
    """The plan names the threshold declarations verbatim: jurisdiction, effective period and
    currency — a bare cutoff refuses with the missing fields listed."""
    spec = POLICY_KIND_REGISTRY["threshold"]
    assert spec.required_declarations == ("jurisdiction", "effective_period", "currency")
    with pytest.raises(PolicyKindError, match="jurisdiction"):
        validate_policy_declaration("threshold", {"currency": "AED"})
    validate_policy_declaration("threshold", {
        "jurisdiction": "AE", "effective_period": "2026-01-01/2026-12-31", "currency": "AED"})


def test_every_authority_ref_field_maps_to_a_kind_totally():
    """BR-6's carriage cannot drift: every AuthorityRefsV2 field, plus the proposal-level
    allocation ref, names its kind — introspected, so adding a ref field fails HERE until its
    kind is declared."""
    schema_fields = {f.name for f in fields(AuthorityRefsV2)}
    assert schema_fields | {"allocation_policy_ref"} == set(AUTHORITY_REF_KINDS)
    for kind in AUTHORITY_REF_KINDS.values():
        assert kind in POLICY_KIND_REGISTRY


def test_refs_refuse_rather_than_approximate():
    assert parse_policy_ref("eligible_status:posted-only") == ("eligible_status", "posted-only")
    with pytest.raises(PolicyKindError, match="not '<kind>:<name>'"):
        parse_policy_ref("posted-only")
    with pytest.raises(PolicyKindError, match="unknown policy kind"):
        parse_policy_ref("vibes:whatever")
    with pytest.raises(PolicyKindError, match="unknown policy kind"):
        validate_policy_declaration("vibes", {})


def test_no_second_store_the_homes_are_the_existing_mechanisms():
    """Resolution homes point at mechanisms that exist: reversal at the eligibility IR (whose
    ReversalMode already refuses unsupported shapes), model_output at BR-7A's registry."""
    from featuregen.data_agent.eligibility import ReversalMode
    assert ReversalMode.BOOLEAN_OR_CODE_COLUMN.value == "boolean_or_code_column"
    assert "eligibility" in POLICY_KIND_REGISTRY["reversal_correction"].resolution_home
    assert "ModelFeatureSpecV1" in POLICY_KIND_REGISTRY["model_output"].resolution_home


def test_a_formulas_shape_names_the_kinds_it_needs():
    assert required_policy_kinds() == ()
    assert required_policy_kinds(filtered=True) == ("eligible_status", "reversal_correction")
    assert required_policy_kinds(filtered=True, monetary=True, per_row_currency=True,
                                 cross_grain_rollup=True) == (
        "eligible_status", "reversal_correction", "direction_sign", "currency_conversion",
        "allocation")


def test_an_undeclared_status_policy_blocks_the_filtered_formula():
    """The acceptance sentence through the REAL fold: a filtered formula with a reviewed,
    grammar-accepted expectation but no declared status/reversal policy folds to
    FORMULA_BLOCKED with the missing kinds NAMED — and declaring them releases it."""
    required = required_policy_kinds(filtered=True)
    blockers = policy_blockers(required, declared=())
    assert blockers == ("policy_undeclared:eligible_status",
                        "policy_undeclared:reversal_correction")
    blocked = fold_readiness(ReadinessInputsV1(
        computation_kind="deterministic_formula", reviewed_expectation=True,
        grammar_verdict="ok", governed_policy_blockers=blockers))
    assert blocked.state == "FORMULA_BLOCKED"
    assert set(blockers) <= set(blocked.blockers)

    released = fold_readiness(ReadinessInputsV1(
        computation_kind="deterministic_formula", reviewed_expectation=True,
        grammar_verdict="ok",
        governed_policy_blockers=policy_blockers(required, declared=required)))
    assert released.state == "FORMULA_AUTHORABLE"
