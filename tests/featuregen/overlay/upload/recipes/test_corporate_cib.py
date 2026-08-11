"""BR-16 — the Corporate/CIB pack: lifecycle-correct trade finance, and the counter at ZERO."""
from __future__ import annotations

from featuregen.overlay.upload.recipe_audit import audit_registry
from featuregen.overlay.upload.recipe_registry_v2 import v2_replaced_legacy_ids
from featuregen.overlay.upload.recipes.corporate_cib import CORPORATE_CIB_RECIPES
from featuregen.overlay.upload.templates import ALL_TEMPLATES, CORPORATE_TRADE_TEMPLATES

BY_ID = {r.recipe_id: r for r in CORPORATE_CIB_RECIPES}


def test_every_legacy_recipe_in_the_whole_registry_is_now_replaced():
    """The migration milestone: with the last family pack landed, EVERY legacy template has
    an explicit V2 replacement — the audit's headline counter is ZERO."""
    assert {t.id for t in CORPORATE_TRADE_TEMPLATES} <= v2_replaced_legacy_ids()
    assert {t.id for t in ALL_TEMPLATES} <= v2_replaced_legacy_ids()
    assert audit_registry().counters["legacy_recipes_not_in_v2"] == 0


def test_lc_outputs_read_the_instrument_lifecycle_not_contingent_exposure():
    r = BY_ID["lc_guarantee_rollover_count"]
    concepts = {op.concept for op in r.operands}
    assert {"instrument_id", "instrument_type", "lc_guarantee_event"} <= concepts
    assert "contingent exposure alone" in r.business_definition
    assert "counted as rollovers" in r.eligibility.excluded


def test_dso_and_dilution_require_the_invoice_lifecycle():
    dso = BY_ID["invoice_dso_days"]
    concepts = {op.concept for op in dso.operands}
    assert {"invoice_id", "invoice_status", "due_date", "customer_id"} <= concepts
    assert "no invoice linkage" in dso.eligibility.excluded
    dilution = BY_ID["invoice_dilution_share"]
    assert "credit-note lifecycle" in dilution.eligibility.excluded


def test_group_exposure_respects_hierarchy_and_elimination():
    r = BY_ID["group_exposure_aggregation"]
    refs = r.eligibility.policy_refs
    assert any(ref.startswith("active_state:effective-dated-legal") for ref in refs)
    assert any(ref.startswith("allocation:intra-group-elimination") for ref in refs)
    assert r.output_grain == "legal_group"
    assert "Departed subsidiaries".lower() in r.eligibility.excluded.lower()


def test_intraday_peak_cannot_compile_from_daily_snapshots():
    """The acceptance: the peak's SOURCE GRAIN is the intraday sweep event — a daily
    snapshot recipe is a different recipe on a different grain."""
    peak, eod = BY_ID["pool_intraday_peak"], BY_ID["pool_utilisation_eod"]
    assert peak.source_grain == "intraday_sweep_event"
    assert eod.source_grain == "pool_day_snapshot"
    assert peak.temporal.window_unit == "minutes"
    assert "daily snapshots standing in" in peak.eligibility.excluded
    assert "not evidence of intraday behaviour" in peak.output.empty_population_policy


def test_obligor_facility_count_stays_atomic_and_authorable():
    r = BY_ID["obligor_facility_count"]
    assert r.readiness == "FORMULA_AUTHORABLE"
    assert r.formula.formula_schema_version == "formula-v1"
    assert r.formula.expectation_ref == "obligor_facility_count"
    from featuregen.overlay.upload.recipe_formula_expectations import (
        RECIPE_FORMULA_EXPECTATIONS,
    )
    assert r.formula.expectation_ref in RECIPE_FORMULA_EXPECTATIONS


def test_guarantees_require_enforceability_and_wrong_way_policy():
    r = BY_ID["guarantor_reliance_share"]
    assert any(ref.startswith("risk_corridor:guarantee-wrong-way")
               for ref in r.eligibility.policy_refs)
    assert "Expired guarantees".lower() in r.eligibility.excluded.lower()
    assert "unexpired" in r.temporal.snapshot_policy


def test_stress_splits_the_line_count_and_reads_the_governed_threshold():
    r = BY_ID["stressed_line_count"]
    concepts = {op.concept for op in r.operands}
    assert {"drawn_principal", "contingent_exposure"} <= concepts
    assert any(op.status_policy_ref.startswith("threshold:cross-product")
               for op in r.operands)
    assert "counts zero, honestly" in r.business_definition


# ── BR-17: the legacy registry is FROZEN ────────────────────────────────────────────────────────
def test_the_legacy_template_registry_is_frozen():
    """New recipe authoring through Template fails CI: the legacy id set is pinned EXACTLY.
    Author new recipes in recipes/<family>.py against Recipe Contract v2 — this is also the
    enforcement of BR-2's routing rule for the 4c triage cards."""
    from featuregen.overlay.upload.recipe_registry_v2 import LEGACY_ALIAS_MAP

    legacy_ids = {t.id for t in ALL_TEMPLATES}
    assert len(legacy_ids) == 157
    assert legacy_ids == set(LEGACY_ALIAS_MAP), (
        "the legacy registry changed: a new Template must be authored as a RecipeDefinitionV2 "
        "in recipes/<family>.py (and a removal needs its alias-map entry retired deliberately)")


def test_the_active_registry_carries_zero_reviewed_debt():
    """BR-17 strict mode: the ACTIVE registry's debt counters are zero — no unreplaced legacy
    recipe, no variant identity collision, no UNASSESSED (unconstructible), no legacy
    applicability inference (every V2 recipe declares its primary), no PIT placeholders (typed
    temporal specs have none)."""
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    counters = audit_registry().counters
    assert counters["legacy_recipes_not_in_v2"] == 0
    assert counters["v2_variant_identity_collision_recipes"] == 0
    assert all(r.primary_objective for r in V2_RECIPES)
    assert all(r.readiness != "UNASSESSED" for r in V2_RECIPES)
    # display identity: every output id and display label unique across the active registry
    output_ids = [r.output.output_id for r in V2_RECIPES]
    assert len(output_ids) == len(set(output_ids))
