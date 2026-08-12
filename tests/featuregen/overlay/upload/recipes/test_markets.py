"""BR-14 — the markets pack: model provenance, enforceable netting, atomic limits."""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_registry import model_feature_by_id
from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.markets import MARKETS_MODEL_FEATURES, MARKETS_RECIPES
from featuregen.overlay.upload.templates import MARKETS_TEMPLATES

BY_ID = {r.recipe_id: r for r in MARKETS_RECIPES}


def test_every_legacy_markets_id_is_explicitly_replaced():
    assert {t.id for t in MARKETS_TEMPLATES} <= v2_replaced_legacy_ids()


def test_var_and_greeks_are_governed_model_outputs_with_provenance():
    """The acceptance: market model outputs preserve complete model/valuation provenance —
    they are governed_model_output recipes over registered risk_model_output specs, and their
    eligibility refuses rows missing any provenance field."""
    for rid, ref in (("position_var_risk", "position_var"),
                     ("greek_sensitivity_exposure", "greek_sensitivities")):
        r = BY_ID[rid]
        assert r.computation_kind == "governed_model_output", rid
        spec = model_feature_by_id(ref)
        assert spec is not None and spec.model_family == "risk_model_output"
        assert spec.model_version == ""                    # honestly unregistered
        assert any(ref2.startswith("model_output:") for ref2 in r.eligibility.policy_refs)
        assert "provenance" in r.eligibility.included
    assert len(MARKETS_MODEL_FEATURES) == 2


def test_netting_requires_enforceability_and_reads_gross_otherwise():
    r = BY_ID["notional_netting_exposure"]
    assert any(op.concept == "netting_set_id" for op in r.operands)
    refs = r.eligibility.policy_refs
    assert any(ref.startswith("allocation:netting-set-legal") for ref in refs)
    assert any(ref.startswith("allocation:csa-effective") for ref in refs)
    assert "reads GROSS" in r.business_definition
    assert "collateral posted after the cutoff" in r.eligibility.excluded.lower() or \
        "after the cutoff" in r.eligibility.excluded


def test_counterparty_recipes_run_at_legal_entity_grain():
    for rid in ("notional_netting_exposure", "counterparty_exposure_trend",
                "margin_call_intensity"):
        r = BY_ID[rid]
        assert r.output_grain == "counterparty", rid
        assert any(op.concept == "lei" for op in r.operands), rid


def test_the_limit_family_is_atomic():
    usage, breach = BY_ID["trading_limit_usage"], BY_ID["trading_limit_breach_count"]
    assert usage.output.unit_kind == "ratio" and breach.output.unit_kind == "count"
    for r in (usage, breach):
        assert any(ref.startswith("threshold:trading-limit-record")
                   for ref in r.eligibility.policy_refs)
    assert set(usage.replaces_legacy_ids) == {"trading_limit_utilisation"}
    assert set(breach.replaces_legacy_ids) == {"trading_limit_utilisation"}


def test_concentration_reads_the_desk_hierarchy():
    r = BY_ID["book_desk_concentration"]
    assert r.output_grain == "desk"
    assert any(ref.startswith("allocation:desk-book-hierarchy")
               for ref in r.eligibility.policy_refs)


def test_basis_dislocation_carries_two_distinct_legs():
    r = BY_ID["benchmark_basis_dislocation"]
    legs = [op for op in r.operands if op.distinct_binding_group == "basis_legs"]
    assert len(legs) == 2
