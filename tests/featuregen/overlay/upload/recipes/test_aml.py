"""BR-13 — the AML pack: jurisdictional thresholds, distinct party legs, lagged alerts."""
from __future__ import annotations

from featuregen.overlay.upload.banking_policies import parse_policy_ref
from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.aml import ALERT_HISTORY_USE, AML_RECIPES
from featuregen.overlay.upload.templates import AML_TEMPLATES

BY_ID = {r.recipe_id: r for r in AML_RECIPES}


def test_every_legacy_aml_id_is_explicitly_replaced():
    assert {t.id for t in AML_TEMPLATES} <= v2_replaced_legacy_ids()


def test_aml_thresholds_are_governed_jurisdictional_policy():
    """The acceptance: effective-dated, jurisdictional, currency-aware — the threshold is a
    policy REF of the BR-10 threshold kind (whose declaration schema requires jurisdiction,
    effective_period and currency), never a number in a recipe."""
    r = BY_ID["structuring_smurfing"]
    threshold = next(op for op in r.operands if op.role == "reporting_threshold")
    kind, _ = parse_policy_ref(threshold.status_policy_ref)
    assert kind == "threshold"
    assert "jurisdictions or effective periods" in r.eligibility.excluded


def test_cash_is_channel_and_instrument_never_purpose_code():
    for rid in ("structuring_smurfing", "cash_intensity_ratio"):
        concepts = {op.concept for op in BY_ID[rid].operands}
        assert {"channel", "instrument_type"} <= concepts, rid
        assert "iso20022_purpose_code" not in concepts, rid
    assert "purpose-code" in BY_ID["round_amount_ratio"].eligibility.excluded


def test_network_recipes_carry_distinct_party_legs():
    """The fan/passthrough legs are distinct binding groups — a source whose one counterparty
    column serves both legs is refused, never merged (the counterparty-canonicalization
    lesson, now contract law)."""
    for rid, group in (("fan_in_fan_out", "fan_legs"),
                       ("rapid_movement_passthrough", "passthrough_legs"),
                       ("nested_correspondent_flow", "correspondent_banks")):
        legs = [op for op in BY_ID[rid].operands if op.distinct_binding_group == group]
        assert len(legs) == 2, rid


def test_nested_correspondent_runs_at_respondent_grain():
    r = BY_ID["nested_correspondent_flow"]
    assert r.output_grain == "respondent_bank"
    assert r.source_grain == "correspondent_payment"
    assert any(op.concept == "nested_correspondent_flag" for op in r.operands)


def test_corridor_and_vasp_classifications_are_effective_dated_policy():
    for rid, kind in (("high_risk_corridor_exposure", "risk_corridor"),
                      ("crypto_offramp_exposure", "risk_corridor")):
        r = BY_ID[rid]
        assert any(parse_policy_ref(ref)[0] == kind for ref in r.eligibility.policy_refs), rid


def test_screening_is_three_separate_facts():
    exposure, alert, match = (BY_ID["screening_exposure_share"],
                              BY_ID["screening_alert_count"], BY_ID["confirmed_match_flag"])
    assert exposure.leakage.classification == "standard"
    assert alert.leakage.classification == "near_label"
    assert match.leakage.classification == "near_label"
    assert {"screening_exposure"} == set(exposure.replaces_legacy_ids)


def test_alert_outcomes_cannot_leak_into_behavior_features():
    """The acceptance: alert/case history is near-label AND read through knowledge time — an
    outcome recorded after the cutoff never informs it."""
    assert "origination" in ALERT_HISTORY_USE.prohibited_stages
    for rid in ("screening_alert_count", "confirmed_match_flag", "prior_alert_recidivism"):
        r = BY_ID[rid]
        assert r.leakage.classification == "near_label", rid
        assert r.temporal.knowledge_time_role == "knowledge_ts", rid
        assert any(op.concept == "system_time" for op in r.operands), rid
