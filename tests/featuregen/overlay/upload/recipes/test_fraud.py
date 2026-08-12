"""BR-13 — the fraud pack: lifecycle stages declared, payee novelty on identity."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.fraud import FRAUD_LIFECYCLE_STAGE, FRAUD_RECIPES
from featuregen.overlay.upload.templates import FRAUD_TEMPLATES

BY_ID = {r.recipe_id: r for r in FRAUD_RECIPES}


def test_every_legacy_fraud_id_is_explicitly_replaced():
    assert {t.id for t in FRAUD_TEMPLATES} <= v2_replaced_legacy_ids()


def test_every_recipe_declares_its_lifecycle_stage_and_reads_that_stages_feed():
    """The acceptance: authorization and settlement are never interchangeable operands. The
    stage map is TOTAL over the pack, and post-authorization recipes read the authorization
    feed's own outcome/timestamp concepts — never a booked-transaction stand-in."""
    assert set(FRAUD_LIFECYCLE_STAGE) == set(BY_ID)
    for rid, stage in FRAUD_LIFECYCLE_STAGE.items():
        r = BY_ID[rid]
        if stage == "post_authorization":
            assert r.source_grain == "authorization_event", rid
            concepts = {op.concept for op in r.operands}
            assert "authorization_status" in concepts, rid
            assert "settlement_status" not in concepts, rid
        if stage == "post_booking" and r.computation_kind == "deterministic_formula":
            assert r.source_grain == "transaction", rid


def test_card_testing_reads_the_authorization_outcome():
    r = BY_ID["card_testing_velocity"]
    assert any(op.concept == "authorization_status" for op in r.operands)
    assert any(op.concept == "authorization_timestamp" for op in r.operands)
    assert "small-amount" in " ".join(r.eligibility.policy_refs) or any(
        "small-amount" in ref for ref in r.eligibility.policy_refs)
    assert r.temporal.window_unit == "minutes"


def test_payee_novelty_is_on_beneficiary_identity_not_bank():
    """The acceptance sentence, literally."""
    r = BY_ID["first_time_payee_high_value"]
    concepts = {op.concept for op in r.operands}
    assert "beneficiary_id" in concepts and "beneficiary_bank" not in concepts
    payee = next(op for op in r.operands if op.concept == "beneficiary_id")
    assert "never the beneficiary bank" in payee.relationship_requirement
    assert any(p.governed_policy_ref.startswith("threshold:") for p in r.parameters
               if p.parameter_class == "governed_policy")


def test_merchant_anomaly_is_customer_relative():
    r = BY_ID["merchant_amount_zscore"]
    assert any(op.concept == "customer_id" and op.operand_class == "entity_key"
               for op in r.operands)
    assert "customer-relative" in r.business_definition


def test_merchant_mcc_diversity_keeps_its_reviewed_v1_expectation():
    """The one honestly-authorable recipe: the Formula-v1 count-distinct expectation is
    retained verbatim as the expectation ref, and the readiness says so."""
    r = BY_ID["merchant_mcc_diversity"]
    assert r.formula.formula_schema_version == "formula-v1"
    assert r.formula.expectation_ref == "merchant_mcc_diversity"
    assert r.readiness == "FORMULA_AUTHORABLE"
    from featuregen.overlay.upload.recipe_formula_expectations import (
        RECIPE_FORMULA_EXPECTATIONS,
    )
    assert r.formula.expectation_ref in RECIPE_FORMULA_EXPECTATIONS


def test_impossible_travel_is_conceptual_with_the_grammar_gap_named():
    r = BY_ID["geo_velocity_impossible"]
    assert r.computation_kind == "conceptual_pattern"
    assert "outside the formula grammar" in r.conceptual_reason


def test_device_recipes_carry_the_continuity_policy():
    for rid in ("device_sharing_velocity", "new_device_flag"):
        assert any(ref.startswith("active_state:card-token-device")
                   for ref in BY_ID[rid].eligibility.policy_refs), rid


def test_just_under_limit_reads_the_governed_control():
    r = BY_ID["amount_just_under_limit"]
    assert any(op.operand_class == "policy_input" and op.status_policy_ref.startswith(
        "threshold:") for op in r.operands)
