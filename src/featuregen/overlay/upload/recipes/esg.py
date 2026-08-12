"""BR-15 — the ESG pack: 9 legacy templates → 9 atomic V2 recipes.

Structural corrections: every recipe carries the reporting BOUNDARY set (reporting entity,
organizational boundary, reporting year, methodology, assurance status) as a governed policy
input — outputs cannot combine incompatible boundaries, years or methodologies because the
boundary IS an operand; scope and category prevent double counting; financed emissions read
the PCAF attribution factor, financed amount and EVIC/revenue basis; physical risk names
scenario, horizon, hazard and asset location; transition alignment names pathway, sector and
target basis; and ABSOLUTE emissions and INTENSITY are separate recipes.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
)
from featuregen.overlay.upload.recipes._shared import (
    as_of,
    dim,
    entity,
    formula,
    measure,
    policy_input,
    snapshot_window,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

SCORING = "esg.scoring"
TRANSITION = "esg.climate.transition"
PHYSICAL = "esg.climate.physical"

BOUNDARY = "risk_corridor:ghg-boundary-methodology-year"
PCAF = "risk_corridor:pcaf-attribution-basis"
SCENARIO = "risk_corridor:climate-scenario-horizon"
PATHWAY = "risk_corridor:transition-pathway-target-basis"


def _boundary(source: str):
    return policy_input("boundary", "reporting_period", source, policy=BOUNDARY)


ESG_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="absolute_emissions_by_scope", revision=1, family="esg",
        primary_objective=SCORING,
        business_definition=(
            "ABSOLUTE emissions per scope for the reporting entity, inside ONE declared "
            "boundary/methodology/year — scope and category prevent double counting."),
        decision_context="emissions accounting (absolute side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="absolute_emissions_by_scope", display_label="Absolute emissions",
            output_type="numeric", additivity="additive", unit_kind="count",
            unit_policy="tCO2e",
            null_input_policy="rows with no scope/category are excluded and surface as a gap",
            empty_population_policy="no reported emissions returns null — absence of data "
                                    "is not zero emissions",
            aggregation_over_entity="sum within ONE boundary and year — never across "
                                    "boundaries, years or methodologies",
            aggregation_over_time="one reporting year at a time"),
        operands=(entity("reporting_entity", "lei", "emissions_report"),
                  measure("emissions", "scope_1_emissions", "emissions_report", unit="count"),
                  dim("scope_category", "category_code", "emissions_report"),
                  _boundary("emissions_report"),
                  as_of("emissions_report")),
        source_grain="emissions_report", output_grain="reporting_entity",
        temporal=snapshot_window("the declared reporting year's final report"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="emissions inside one declared boundary, methodology and year",
            excluded="mixing boundaries, years or methodologies in one number",
            policy_refs=(BOUNDARY,)),
        formula=formula("esg", "absolute_emissions_by_scope", "sum"),
        replaces_legacy_ids=("emissions_trend_by_scope",)),
    RecipeDefinitionV2(
        recipe_id="carbon_intensity_trajectory", revision=1, family="esg",
        primary_objective=TRANSITION,
        business_definition=(
            "OLS slope of carbon INTENSITY (emissions per revenue/EVIC basis) across "
            "reporting years — intensity, separate from absolute emissions."),
        decision_context="intensity trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="carbon_intensity_trajectory", display_label="Intensity trajectory",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="tCO2e per revenue unit per year",
            null_input_policy="years missing either side are excluded from the fit",
            empty_population_policy="fewer than two reporting years returns null"),
        operands=(entity("reporting_entity", "lei", "emissions_report"),
                  measure("intensity", "carbon_intensity", "emissions_report", unit="rate"),
                  _boundary("emissions_report"),
                  as_of("emissions_report")),
        source_grain="emissions_report", output_grain="reporting_entity",
        temporal=snapshot_window("one final report per year, same boundary across years"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="intensity on one declared basis across comparable years",
            excluded="mixing bases or restated years without the restatement",
            policy_refs=(BOUNDARY, PCAF)),
        formula=formula("esg", "carbon_intensity_trajectory", "slope"),
        replaces_legacy_ids=("carbon_intensity_trajectory",)),
    RecipeDefinitionV2(
        recipe_id="financed_emissions_attribution", revision=1, family="esg",
        primary_objective=SCORING,
        business_definition=(
            "The portfolio's financed emissions: borrower emissions × the PCAF attribution "
            "factor (financed amount over EVIC/revenue basis), with the data-quality score "
            "carried beside the number."),
        decision_context="financed emissions",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="financed_emissions_attribution", display_label="Financed emissions",
            output_type="numeric", additivity="additive", unit_kind="count",
            unit_policy="tCO2e",
            null_input_policy="exposures with no attribution basis are excluded and surface "
                              "as a gap",
            empty_population_policy="no attributable exposure returns null",
            aggregation_over_entity="sum within one attribution basis and year",
            aggregation_over_time="one reporting year at a time"),
        operands=(entity("portfolio", "portfolio_id", "financed_emissions_row"),
                  measure("financed_emissions", "financed_emissions",
                          "financed_emissions_row", unit="count"),
                  measure("data_quality", "emissions_data_quality",
                          "financed_emissions_row", unit="score"),
                  policy_input("attribution", "ownership_percentage",
                               "financed_emissions_row", policy=PCAF),
                  as_of("financed_emissions_row")),
        source_grain="financed_emissions_row", output_grain="portfolio",
        temporal=snapshot_window("the declared reporting year's attribution run"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="exposures with PCAF attribution factor, financed amount and basis",
            excluded="mixing attribution bases in one number",
            policy_refs=(PCAF, BOUNDARY)),
        formula=formula("esg", "financed_emissions_attribution", "sum"),
        replaces_legacy_ids=("financed_emissions_attribution",)),
    RecipeDefinitionV2(
        recipe_id="emissions_data_quality_reliance", revision=1, family="esg",
        primary_objective=SCORING,
        business_definition=(
            "Share of financed emissions resting on LOW-QUALITY (proxy/estimated) data, by "
            "PCAF data-quality score."),
        decision_context="data-quality reliance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="emissions_data_quality_reliance", display_label="Low-quality reliance",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with no quality score are counted LOW quality, never "
                              "dropped",
            empty_population_policy="no financed emissions returns null",
            zero_denominator_policy="zero financed emissions returns null"),
        operands=(entity("portfolio", "portfolio_id", "financed_emissions_row"),
                  measure("financed_emissions", "financed_emissions",
                          "financed_emissions_row", unit="count"),
                  measure("data_quality", "emissions_data_quality",
                          "financed_emissions_row", unit="score"),
                  as_of("financed_emissions_row")),
        source_grain="financed_emissions_row", output_grain="portfolio",
        temporal=snapshot_window("the declared reporting year's attribution run"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="attribution rows with quality scores",
            excluded="quality-unknown rows silently counted as high quality",
            policy_refs=(PCAF,)),
        formula=formula("esg", "emissions_data_quality_reliance", "share"),
        replaces_legacy_ids=("emissions_data_quality_reliance",)),
    RecipeDefinitionV2(
        recipe_id="taxonomy_alignment_share", revision=1, family="esg",
        primary_objective=SCORING,
        business_definition=(
            "Taxonomy-aligned exposure as a share of eligible exposure, inside one declared "
            "methodology year."),
        decision_context="taxonomy alignment",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="taxonomy_alignment_share", display_label="Taxonomy alignment",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="exposures with no eligibility assessment are excluded from "
                              "both sides",
            empty_population_policy="no eligible exposure returns null",
            zero_denominator_policy="zero eligible exposure returns null"),
        operands=(entity("portfolio", "portfolio_id", "alignment_row"),
                  measure("aligned", "taxonomy_alignment", "alignment_row", unit="rate"),
                  _boundary("alignment_row"),
                  as_of("alignment_row")),
        source_grain="alignment_row", output_grain="portfolio",
        temporal=snapshot_window("one methodology year's assessment"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="assessed exposures inside one methodology year",
            excluded="mixing methodology years",
            policy_refs=(BOUNDARY,)),
        formula=formula("esg", "taxonomy_alignment_share", "share"),
        replaces_legacy_ids=("taxonomy_alignment_share",)),
    RecipeDefinitionV2(
        recipe_id="transition_alignment_gap", revision=1, family="esg",
        primary_objective=TRANSITION,
        business_definition=(
            "The portfolio's intensity against its transition PATHWAY target — pathway, "
            "sector and target basis declared; the gap is basis-specific."),
        decision_context="transition alignment",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="transition_alignment_gap", display_label="Transition gap",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="sectors with no pathway target return null and surface the gap",
            empty_population_policy="no pathway coverage returns null",
            zero_denominator_policy="a zero target returns null"),
        operands=(entity("portfolio", "portfolio_id", "alignment_row"),
                  measure("intensity", "carbon_intensity", "alignment_row", unit="rate"),
                  dim("sector", "industry_code", "alignment_row"),
                  policy_input("pathway", "transition_alignment", "alignment_row",
                               policy=PATHWAY),
                  as_of("alignment_row")),
        source_grain="alignment_row", output_grain="portfolio",
        temporal=snapshot_window("the pathway vintage effective at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="sector intensities against the declared pathway and target basis",
            excluded="pathway substitution mid-comparison",
            policy_refs=(PATHWAY, BOUNDARY)),
        formula=formula("esg", "transition_alignment_gap", "ratio"),
        replaces_legacy_ids=("transition_alignment_gap",)),
    RecipeDefinitionV2(
        recipe_id="physical_hazard_exposure", revision=1, family="esg",
        primary_objective=PHYSICAL,
        business_definition=(
            "Exposure-weighted physical hazard score under a DECLARED scenario, horizon and "
            "hazard, over located assets — scenario facts, never scenario-free averages."),
        decision_context="physical climate risk",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="physical_hazard_exposure", display_label="Physical hazard exposure",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="assets with no location are excluded and surface as a gap",
            empty_population_policy="no located assets returns null"),
        operands=(entity("portfolio", "portfolio_id", "hazard_row"),
                  measure("hazard", "physical_hazard_score", "hazard_row", unit="score"),
                  dim("location", "geographic", "hazard_row"),
                  policy_input("scenario", "scenario_id", "hazard_row", policy=SCENARIO),
                  as_of("hazard_row")),
        source_grain="hazard_row", output_grain="portfolio",
        temporal=snapshot_window("the scenario run effective at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="located assets under one declared scenario/horizon/hazard",
            excluded="mixing scenarios or horizons in one score",
            policy_refs=(SCENARIO,)),
        formula=formula("esg", "physical_hazard_exposure", "ratio"),
        replaces_legacy_ids=("physical_hazard_exposure",)),
    RecipeDefinitionV2(
        recipe_id="sll_kpi_achievement", revision=1, family="esg",
        primary_objective=SCORING,
        business_definition=(
            "Sustainability-linked-loan KPIs met divided by KPIs due for testing in the "
            "window, per facility."),
        decision_context="SLL performance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="sll_kpi_achievement", display_label="SLL KPI achievement",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="KPIs with no test result are excluded and surface as a gap",
            empty_population_policy="no KPIs due returns null",
            zero_denominator_policy="zero KPIs due returns null"),
        operands=(entity("facility", "facility_id", "sll_test_event"),
                  measure("kpi", "sll_kpi", "sll_test_event", unit="score"),
                  as_of("sll_test_event")),
        source_grain="sll_test_event", output_grain="facility",
        temporal=snapshot_window("KPI tests due in the window"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="KPIs due for testing under the loan's declared targets",
            excluded="untested KPIs counted as met",
            policy_refs=("threshold:sll-declared-targets",)),
        formula=formula("esg", "sll_kpi_achievement", "share"),
        replaces_legacy_ids=("sll_kpi_achievement",)),
    RecipeDefinitionV2(
        recipe_id="scope3_value_chain_exposure", revision=1, family="esg",
        primary_objective=SCORING,
        business_definition=(
            "Scope-3 emissions by value-chain category inside one boundary — the category "
            "IS the double-counting guard."),
        decision_context="value-chain emissions",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="scope3_value_chain_exposure", display_label="Scope-3 by category",
            output_type="numeric", additivity="additive", unit_kind="count",
            unit_policy="tCO2e",
            null_input_policy="rows with no category are excluded and surface as a gap",
            empty_population_policy="no scope-3 reporting returns null",
            aggregation_over_entity="sum within one category and boundary — categories "
                                    "never sum across boundaries",
            aggregation_over_time="one reporting year at a time"),
        operands=(entity("reporting_entity", "lei", "emissions_report"),
                  measure("scope3", "scope_3_emissions", "emissions_report", unit="count"),
                  dim("category", "category_code", "emissions_report"),
                  _boundary("emissions_report"),
                  as_of("emissions_report")),
        source_grain="emissions_report", output_grain="reporting_entity",
        temporal=snapshot_window("the declared reporting year's final report"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="categorized scope-3 rows inside one boundary and year",
            excluded="cross-boundary or cross-year mixing",
            policy_refs=(BOUNDARY,)),
        formula=formula("esg", "scope3_value_chain_exposure", "sum"),
        replaces_legacy_ids=("scope3_value_chain_exposure",)),
)
