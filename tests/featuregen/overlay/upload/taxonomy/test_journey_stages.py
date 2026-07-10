"""Phase-2A Task A1 — tests for the OPTIONAL controlled journey vocabulary."""
from __future__ import annotations

from itertools import combinations

import pytest

from featuregen.overlay.upload.taxonomy.journey_stages import (
    JOURNEY_MODELS,
    JourneyMetadata,
    journey_metadata,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_BY_ID = {t.id: t for t in ALL_TEMPLATES}


# ── totality + the one-directional invariant ──────────────────────────────────────────────────────
def test_journey_metadata_total_over_all_templates():
    # Every recipe yields a JourneyMetadata; a set stage is always a member of a set model.
    for t in ALL_TEMPLATES:
        jm = journey_metadata(t)
        assert isinstance(jm, JourneyMetadata)
        if jm.journey_stage_id is not None:
            assert jm.journey_model_id is not None
            assert jm.journey_stage_id in JOURNEY_MODELS[jm.journey_model_id].stages


def test_journey_is_optional_not_forced():
    # A meaningful chunk of recipes has NO journey — there is deliberately no "153/153" rule.
    with_journey = sum(1 for t in ALL_TEMPLATES if journey_metadata(t).journey_model_id is not None)
    without = len(ALL_TEMPLATES) - with_journey
    assert with_journey > 0
    assert without > 0
    assert with_journey < len(ALL_TEMPLATES)


# ── genuine funnels resolve ───────────────────────────────────────────────────────────────────────
def test_churn_recipe_resolves_to_customer_attrition():
    jm = journey_metadata(_BY_ID["balance_trend"])
    assert jm.journey_model_id == "customer_attrition"
    assert jm.journey_stage_id == "financial_migration"
    assert journey_metadata(_BY_ID["dd_cancellation_rate"]).journey_stage_id == "unbundling"


def test_credit_recipe_resolves_to_credit_deterioration():
    jm = journey_metadata(_BY_ID["credit_utilisation"])
    assert jm.journey_model_id == "credit_deterioration"
    assert jm.journey_stage_id == "early_stress"
    assert journey_metadata(_BY_ID["forbearance_in_window"]).journey_stage_id == "default"


def test_each_declared_funnel_model_is_reached_by_at_least_one_recipe():
    reached = {journey_metadata(t).journey_model_id for t in ALL_TEMPLATES}
    reached.discard(None)
    assert reached == {
        "customer_attrition", "credit_deterioration", "collections", "fraud_kill_chain",
        "aml_cycle", "deposit_stability", "redemption", "insurance_lapse",
    }


# ── non-funnel recipes stay null (not forced) ─────────────────────────────────────────────────────
def test_non_funnel_recipes_have_no_journey():
    # ESG scoring, a mandate-compliance measure, an actuarial recipe: no funnel -> both null.
    for tid in ("emissions_trend_by_scope", "mandate_breach_proximity", "mortality_morbidity_loading"):
        jm = journey_metadata(_BY_ID[tid])
        assert jm == JourneyMetadata(None, None)


def test_funnel_family_on_a_non_funnel_stage_is_not_forced():
    # retail_churn recipes that sit on baseline/context stages, and a redemption recipe on a
    # disengagement stage the redemption model does not map, all stay null rather than being forced.
    for tid in ("dormancy_days", "rfm_composite", "tenure_days", "net_fund_flow_trend"):
        assert journey_metadata(_BY_ID[tid]) == JourneyMetadata(None, None)


# ── membership: every emitted stage is a member of its model ───────────────────────────────────────
def test_every_emitted_stage_is_a_member_of_its_model():
    for t in ALL_TEMPLATES:
        jm = journey_metadata(t)
        if jm.journey_model_id is not None:
            assert jm.journey_stage_id in JOURNEY_MODELS[jm.journey_model_id].stages


# ── the invariant rejects invalid pairings ────────────────────────────────────────────────────────
def test_stage_without_model_is_rejected():
    with pytest.raises(ValueError):
        JourneyMetadata(journey_model_id=None, journey_stage_id="arrears")


def test_stage_not_a_member_of_its_model_is_rejected():
    with pytest.raises(ValueError):
        JourneyMetadata(journey_model_id="credit_deterioration", journey_stage_id="cash_out")
    with pytest.raises(ValueError):
        JourneyMetadata(journey_model_id="not_a_real_model", journey_stage_id="arrears")


def test_both_null_is_valid():
    jm = JourneyMetadata(None, None)
    assert jm.journey_model_id is None
    assert jm.journey_stage_id is None


# ── registry invariants ───────────────────────────────────────────────────────────────────────────
def test_use_case_selectors_are_disjoint():
    # A primary use-case must select a UNIQUE model, else resolution would be order-dependent.
    models = list(JOURNEY_MODELS.values())
    for a, b in combinations(models, 2):
        assert not (a.use_cases & b.use_cases), (a.model_id, b.model_id)


def test_every_stage_map_target_is_a_declared_member():
    for model in JOURNEY_MODELS.values():
        for raw_stage, stage_id in model.stage_map.items():
            assert stage_id in model.stages, (model.model_id, raw_stage, stage_id)
