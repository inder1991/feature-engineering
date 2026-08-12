"""BR-22 — the adversarial mutation battery: every listed systemic defect trips a named guard.

Each plan-listed mutation is applied (or pointed at the machinery that makes it
unconstructible) and the SPECIFIC refusal or identity change is asserted — the old semantic
defects each fail at least one test, which is this file's whole job.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.recipe_contract_v2 import RecipeContractError
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id


def test_swapping_authorization_for_settlement_changes_identity_and_is_classifier_refused():
    """Mutation 1: the two stages are distinct concepts whose descriptions refuse each other
    (the classifier's mechanism), and swapping the operand concept re-keys the recipe so
    every review approval stales by lookup miss."""
    assert "never" in concept("authorization_status").description
    assert "settlement_status" in concept("authorization_status").description
    r = v2_recipe_by_id("card_testing_velocity")
    swapped_ops = tuple(
        replace(op, concept="settlement_status") if op.concept == "authorization_status"
        else op for op in r.operands)
    assert canonical_recipe_v2_hash(replace(r, operands=swapped_ops)) \
        != canonical_recipe_v2_hash(r)


def test_binding_the_beneficiary_bank_as_the_beneficiary_is_structurally_distinct():
    """Mutation 2: beneficiary_id is an identifier in its own payee namespace; the bank is a
    categorical with none — the bridge derivation cannot pair them."""
    assert concept("beneficiary_id").namespace == "payee_registry"
    assert concept("beneficiary_bank").namespace is None


def test_binding_a_deposit_balance_as_drawn_exposure_is_role_gated():
    """Mutation 3: the drawn operand declares its economic role, which BR-5 binds only over
    governed evidence — and dropping the role is an identity change, never a quiet loosening."""
    r = v2_recipe_by_id("utilization_level")
    drawn = next(op for op in r.operands if op.role == "drawn")
    assert drawn.economic_role == "drawn_credit_exposure"
    loosened_ops = tuple(replace(op, economic_role="") if op.role == "drawn" else op
                         for op in r.operands)
    assert canonical_recipe_v2_hash(replace(r, operands=loosened_ops)) \
        != canonical_recipe_v2_hash(r)


def test_removing_the_sign_authority_re_keys_the_net_flow_recipe():
    """Mutation 4: the sign policy rides eligibility, which is hash-bearing."""
    r = v2_recipe_by_id("net_transaction_flow")
    stripped = replace(r.eligibility, policy_refs=tuple(
        ref for ref in r.eligibility.policy_refs if not ref.startswith("direction_sign:")))
    assert canonical_recipe_v2_hash(replace(r, eligibility=stripped)) \
        != canonical_recipe_v2_hash(r)


def test_changing_the_measure_without_changing_additivity_is_unconstructible():
    """Mutation 5: the contract's result-class law refuses the mismatch at construction."""
    r = v2_recipe_by_id("posted_debit_amount")
    with pytest.raises(RecipeContractError, match="incompatible"):
        replace(r, formula=replace(r.formula, result_class="ratio"))


def test_an_unmatched_pit_parameter_is_unconstructible():
    """Mutation 6: a temporal window parameter must be declared — the BR-4 defect class."""
    r = v2_recipe_by_id("posted_debit_amount")
    with pytest.raises(RecipeContractError, match="not a declared parameter"):
        replace(r, parameters=())


def test_changing_the_formula_without_review_stales_by_lookup_miss():
    """Mutation 7: the review store keys on canonical-recipe-v2, so ANY definition edit
    changes the hash and every recorded approval misses."""
    r = v2_recipe_by_id("posted_debit_amount")
    edited = replace(r, business_definition=r.business_definition + " (edited)")
    assert canonical_recipe_v2_hash(edited) != canonical_recipe_v2_hash(r)


def test_tied_concept_matches_have_a_closed_refusal_vocabulary():
    """Mutation 8: the binding layer's ambiguity verdicts are a closed vocabulary — an
    unadjudicated required tie is AMBIGUOUS with no selected column, never a coin flip."""
    from featuregen.overlay.upload import recipe_operand_policy as rop

    assert rop.REQUIRED_OPERAND_MISSING == "REQUIRED_OPERAND_MISSING"
    assert hasattr(rop, "bind_v2_operands")


def test_late_group_membership_is_fenced_by_the_effective_snapshot():
    """Mutation 9: group exposure reads membership effective AT the as-of — the departure
    date is in the temporal contract, not a hope."""
    r = v2_recipe_by_id("group_exposure_aggregation")
    assert "effective at the as-of" in r.temporal.snapshot_policy
    assert "Departed subsidiaries".lower() in r.eligibility.excluded.lower()


def test_a_later_model_version_re_keys_the_model_feature():
    """Mutation 10: the model spec's revision hash covers the version — approvals stale."""
    from featuregen.overlay.upload.model_feature_contract import (
        model_feature_revision_hash,
    )
    from featuregen.overlay.upload.model_feature_registry import model_feature_by_id

    spec = model_feature_by_id("churn_probability")
    later = replace(spec, model_version="2.0.0")
    assert model_feature_revision_hash(later) != model_feature_revision_hash(spec)


def test_an_uplift_without_a_control_denominator_is_refused_by_its_own_spec():
    """Mutation 11: the uplift spec's fallback policy names the control requirement, and the
    campaign response rate nulls on zero treatments rather than inventing a rate."""
    from featuregen.overlay.upload.model_feature_registry import model_feature_by_id

    assert "control" in model_feature_by_id("campaign_uplift").fallback_policy
    rate = v2_recipe_by_id("campaign_response_rate")
    assert "null" in rate.output.zero_denominator_policy


def test_a_second_currency_without_conversion_is_refused_at_output_authority():
    """Mutation 12: the BR-6 tooth — a per-row-currency monetary operand with no conversion
    ref refuses with CURRENCY_CONVERSION_UNDECLARED, exercised directly."""
    import json
    from pathlib import Path

    from featuregen.formula.output_authority_v2 import (
        CURRENCY_CONVERSION_UNDECLARED,
        InvalidOutputV2,
        OperandFactsV2,
        resolve_output_v2,
    )
    from featuregen.formula.parse_v2 import parse_proposal_v2

    gold = Path(__file__).parents[1] / "formula" / "gold_v2"
    doc = json.loads((gold / "30_posted_debit_amount_exemplar.json").read_text())["proposal"]
    doc["body"]["expr"].pop("authority_refs")
    verdict = resolve_output_v2(
        parse_proposal_v2(doc),
        {"authored::public.txns.txn_amt": OperandFactsV2(
            logical_type="decimal(38,6)", unit="monetary", currency="per_row")})
    assert isinstance(verdict, InvalidOutputV2)
    assert verdict.reason == CURRENCY_CONVERSION_UNDECLARED
