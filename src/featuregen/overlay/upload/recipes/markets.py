"""BR-14 — the markets pack: 9 legacy templates → 11 atomic V2 recipes + 2 model features.

The corrections, structural: VaR, expected shortfall and model Greeks are GOVERNED MODEL
OUTPUTS — registered ModelFeatureSpecs whose consumption requires complete valuation/model
provenance (valuation timestamp, model version, horizon, confidence, currency, scenario),
never raw deterministic aggregates; notional netting nets ONLY inside a netting set with legal
enforceability and effective credit support (CSA/collateral) declared — an unenforceable
agreement reads GROSS; counterparty exposure runs at counterparty/legal-entity grain; the
limit family splits amount, usage and breach into atomic outputs against the governed limit
record; and desk/book concentration reads the declared hierarchy allocation.
"""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_contract import ModelFeatureSpecV1
from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    OperandSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

PORTFOLIO = "markets.market_risk.portfolio"
BASIS = "markets.market_risk.basis_risk"
CPTY_EXPOSURE = "counterparty_risk.exposure_monitoring"
MARGIN_RISK = "counterparty_risk.margin_call_risk"
CONCENTRATION = "portfolio_risk.concentration"
CPTY_EWI = "counterparty_risk.exposure_monitoring"

VAR_PROVENANCE = "model_output:market-var-engine"
NETTING_ENFORCEABILITY = "allocation:netting-set-legal-enforceability"
CSA_COLLATERAL = "allocation:csa-effective-credit-support"
LIMIT_RECORD = "threshold:trading-limit-record"
DESK_HIERARCHY = "allocation:desk-book-hierarchy"
MKT_BASE_CCY = "currency_conversion:markets-base-currency"

#: BR-7A reclassifications: model-produced risk measures get governed homes. Unversioned —
#: MODEL_SPEC_BLOCKED honestly, until a real engine registration arrives.
MARKETS_MODEL_FEATURES: tuple[ModelFeatureSpecV1, ...] = (
    ModelFeatureSpecV1(
        model_feature_id="position_var", revision=1,
        model_family="risk_model_output", model_ref="models/market-var",
        model_version="", owner="market-risk",
        prediction_grain="book", prediction_timestamp_role="valuation_timestamp",
        training_data_cutoff_policy="market data strictly at or before valuation",
        inference_knowledge_time_policy="positions and marks as known at valuation",
        target_definition="loss quantile at the declared confidence over the declared horizon",
        outcome_window_days=1,
        input_feature_set_revision="pending-first-registered-engine",
        score_type="amount",
        fallback_policy="no VaR — never a raw aggregate stand-in"),
    ModelFeatureSpecV1(
        model_feature_id="greek_sensitivities", revision=1,
        model_family="risk_model_output", model_ref="models/greeks-engine",
        model_version="", owner="market-risk",
        prediction_grain="position", prediction_timestamp_role="valuation_timestamp",
        training_data_cutoff_policy="market data strictly at or before valuation",
        inference_knowledge_time_policy="instrument terms and marks as known at valuation",
        target_definition="first/second-order sensitivities of position value to market factors",
        outcome_window_days=0,
        input_feature_set_revision="pending-first-registered-engine",
        score_type="amount",
        fallback_policy="no Greeks — never a finite-difference guess over stale marks"),
)


def _counterparty(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="counterparty", concept="lei", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _as_of(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="as_of_date", concept="as_of_date",
                         operand_class="as_of_timestamp", allowed_source_grains=(source,))


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"markets:{output_id}",
                              result_class=result_class)


_POSITION_SNAPSHOT = TemporalSpecV2(
    anchor_kind="as_of", business_effective_role="as_of_date",
    window_basis="trailing", window_unit="days", window_parameter="window",
    cutoff_inclusivity="inclusive",
    snapshot_policy="latest-known position snapshot at or before the cutoff")


MARKETS_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── VaR and Greeks are MODEL outputs with full provenance (corrections 1-2, 7) ──────────────
    RecipeDefinitionV2(
        recipe_id="position_var_risk", revision=1, family="markets",
        primary_objective=PORTFOLIO,
        business_definition=(
            "Value-at-risk for the book — a governed MODEL output whose every row carries "
            "valuation timestamp, model version, horizon, confidence level, currency and "
            "scenario; rows with different confidence/horizon/model versions are different "
            "facts, never averaged together."),
        decision_context="market-risk measurement",
        computation_kind="governed_model_output",
        model_feature_ref="position_var",
        output=OutputSpecV2(
            output_id="position_var_risk", display_label="Position VaR",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(
            OperandSpecV2(role="book", concept="book_id", operand_class="entity_key",
                          allowed_source_grains=("risk_measure",)),),
        source_grain="risk_measure", output_grain="book",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
        eligibility=EligibilitySpecV2(
            included="VaR rows with complete valuation/model provenance",
            excluded="rows missing model version, horizon, confidence, currency or scenario",
            policy_refs=(VAR_PROVENANCE,)),
        replaces_legacy_ids=("position_var_risk",)),
    RecipeDefinitionV2(
        recipe_id="greek_sensitivity_exposure", revision=1, family="markets",
        primary_objective=PORTFOLIO,
        business_definition=(
            "Model-produced Greeks per position — instrument, position, valuation and model "
            "provenance all required for consumption."),
        decision_context="sensitivity measurement",
        computation_kind="governed_model_output",
        model_feature_ref="greek_sensitivities",
        output=OutputSpecV2(
            output_id="greek_sensitivity_exposure", display_label="Greek sensitivities",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(
            OperandSpecV2(role="position", concept="instrument_id", operand_class="entity_key",
                          allowed_source_grains=("risk_measure",)),),
        source_grain="risk_measure", output_grain="position",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
        eligibility=EligibilitySpecV2(
            included="Greek rows with instrument, position, valuation and model provenance",
            excluded="rows missing any provenance field",
            policy_refs=(VAR_PROVENANCE,)),
        replaces_legacy_ids=("greek_sensitivity_exposure",)),

    # ── netting nets only inside an enforceable set (corrections 3-4) ───────────────────────────
    RecipeDefinitionV2(
        recipe_id="notional_netting_exposure", revision=1, family="markets",
        primary_objective=CPTY_EXPOSURE,
        business_definition=(
            "Net exposure per counterparty netting SET where the agreement is legally "
            "enforceable with effective credit support (CSA, collateral) — an unenforceable "
            "netting agreement reads GROSS, never net."),
        decision_context="counterparty exposure netting",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="notional_netting_exposure", display_label="Netting-set exposure",
            output_type="numeric", additivity="semi_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=MKT_BASE_CCY,
            null_input_policy="positions with no netting-set membership read gross",
            empty_population_policy="no positions returns zero",
            aggregation_over_entity="net within an enforceable set; gross across sets",
            aggregation_over_time="latest snapshot only"),
        operands=(_counterparty("position_snapshot"),
                  OperandSpecV2(role="netting_set", concept="netting_set_id",
                                operand_class="dimension",
                                allowed_source_grains=("position_snapshot",)),
                  OperandSpecV2(role="exposure", concept="notional", operand_class="measure",
                                allowed_source_grains=("position_snapshot",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="collateral", concept="collateral_value",
                                operand_class="measure", required=False,
                                allowed_source_grains=("position_snapshot",),
                                unit_expectation="monetary"),
                  _as_of("position_snapshot")),
        source_grain="position_snapshot", output_grain="counterparty",
        temporal=_POSITION_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="positions in netting sets with legal enforceability and effective "
                     "credit support declared",
            excluded="netting across unenforceable agreements; collateral posted after the "
                     "cutoff",
            policy_refs=(NETTING_ENFORCEABILITY, CSA_COLLATERAL, MKT_BASE_CCY)),
        formula=_formula("notional_netting_exposure", "snapshot"),
        replaces_legacy_ids=("notional_netting_exposure",)),

    RecipeDefinitionV2(
        recipe_id="counterparty_exposure_trend", revision=1, family="markets",
        primary_objective=CPTY_EXPOSURE,
        business_definition=(
            "OLS slope of net counterparty exposure over the window's snapshots, at "
            "counterparty/legal-entity grain."),
        decision_context="exposure trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="counterparty_exposure_trend", display_label="Exposure trend",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="base currency units per day", currency_policy=MKT_BASE_CCY,
            null_input_policy="days with no snapshot use the latest prior snapshot",
            empty_population_policy="fewer than two snapshot days returns null"),
        operands=(_counterparty("position_snapshot"),
                  OperandSpecV2(role="exposure", concept="expected_exposure",
                                operand_class="measure",
                                allowed_source_grains=("position_snapshot",),
                                unit_expectation="monetary"),
                  _as_of("position_snapshot")),
        source_grain="position_snapshot", output_grain="counterparty",
        temporal=_POSITION_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="exposure snapshots at counterparty/legal-entity grain",
            excluded="account-grain aggregation of a counterparty fact",
            policy_refs=(MKT_BASE_CCY,)),
        formula=_formula("counterparty_exposure_trend", "slope"),
        replaces_legacy_ids=("counterparty_exposure_trend",)),
    RecipeDefinitionV2(
        recipe_id="margin_call_intensity", revision=1, family="markets",
        primary_objective=MARGIN_RISK,
        business_definition=(
            "Count of margin calls issued to the counterparty in the window, under effective "
            "credit support."),
        decision_context="margin stress",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="margin_call_intensity", display_label="Margin call count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="calls with unknown state are excluded",
            empty_population_policy="no calls returns zero"),
        operands=(_counterparty("margin_event"),
                  OperandSpecV2(role="margin_call", concept="margin", operand_class="measure",
                                allowed_source_grains=("margin_event",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="event_ts", concept="event_timestamp",
                                operand_class="event_timestamp",
                                allowed_source_grains=("margin_event",))),
        source_grain="margin_event", output_grain="counterparty",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="margin calls under an effective CSA",
            excluded="calls under unenforceable agreements",
            policy_refs=(CSA_COLLATERAL,)),
        formula=_formula("margin_call_intensity", "count"),
        replaces_legacy_ids=("margin_call_intensity",)),

    # ── the limit family: amount, usage and breach are atomic (correction 5) ────────────────────
    RecipeDefinitionV2(
        recipe_id="trading_limit_usage", revision=1, family="markets",
        primary_objective=PORTFOLIO,
        business_definition=(
            "Book utilization against the governed trading limit record (limit amount and "
            "type read from the record, at the SAME as-of)."),
        decision_context="limit monitoring (usage side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="trading_limit_usage", display_label="Limit usage",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="books missing either side at the as-of return null",
            empty_population_policy="no limit record returns null",
            zero_denominator_policy="a zero limit returns null"),
        operands=(
            OperandSpecV2(role="book", concept="book_id", operand_class="entity_key",
                          allowed_source_grains=("position_snapshot",)),
            OperandSpecV2(role="usage", concept="monetary_stock", operand_class="measure",
                          allowed_source_grains=("position_snapshot",),
                          unit_expectation="monetary"),
            OperandSpecV2(role="limit_amount", concept="limit", operand_class="policy_input",
                          allowed_source_grains=("position_snapshot",),
                          status_policy_ref=LIMIT_RECORD),
            _as_of("position_snapshot")),
        source_grain="position_snapshot", output_grain="book",
        temporal=_POSITION_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="usage against the limit record effective at the as-of",
            excluded="usage against an undated limit",
            policy_refs=(LIMIT_RECORD, MKT_BASE_CCY)),
        formula=_formula("trading_limit_usage", "ratio"),
        replaces_legacy_ids=("trading_limit_utilisation",)),
    RecipeDefinitionV2(
        recipe_id="trading_limit_breach_count", revision=1, family="markets",
        primary_objective=PORTFOLIO,
        business_definition=(
            "Count of limit BREACH events for the book in the window — a breach is an event "
            "against the effective limit record, not usage above a remembered number."),
        decision_context="limit monitoring (breach side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="trading_limit_breach_count", display_label="Limit breaches",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="events with no effective limit record are excluded and "
                              "surface as a data gap",
            empty_population_policy="no breaches returns zero"),
        operands=(
            OperandSpecV2(role="book", concept="book_id", operand_class="entity_key",
                          allowed_source_grains=("limit_event",)),
            OperandSpecV2(role="breach", concept="limit_type", operand_class="status",
                          allowed_source_grains=("limit_event",),
                          status_policy_ref=LIMIT_RECORD),
            OperandSpecV2(role="event_ts", concept="event_timestamp",
                          operand_class="event_timestamp",
                          allowed_source_grains=("limit_event",))),
        source_grain="limit_event", output_grain="book",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="breach events against the limit record effective at event time",
            excluded="usage-derived breach guesses",
            policy_refs=(LIMIT_RECORD,)),
        formula=_formula("trading_limit_breach_count", "count"),
        replaces_legacy_ids=("trading_limit_utilisation",)),

    # ── concentration over the declared hierarchy (correction 6) ────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="book_desk_concentration", revision=1, family="markets",
        primary_objective=CONCENTRATION,
        business_definition=(
            "Concentration (HHI) of exposure across books within the desk, under the "
            "declared desk/book hierarchy allocation."),
        decision_context="desk concentration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="book_desk_concentration", display_label="Desk concentration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="positions with no book assignment are excluded and surface "
                              "as a data gap",
            empty_population_policy="no exposure returns null",
            zero_denominator_policy="zero exposure returns null"),
        operands=(
            OperandSpecV2(role="desk", concept="desk_id", operand_class="entity_key",
                          allowed_source_grains=("position_snapshot",)),
            OperandSpecV2(role="book", concept="book_id", operand_class="dimension",
                          allowed_source_grains=("position_snapshot",)),
            OperandSpecV2(role="exposure", concept="monetary_stock", operand_class="measure",
                          allowed_source_grains=("position_snapshot",),
                          unit_expectation="monetary"),
            _as_of("position_snapshot")),
        source_grain="position_snapshot", output_grain="desk",
        temporal=_POSITION_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="positions allocated through the declared desk/book hierarchy",
            excluded="positions outside the hierarchy",
            policy_refs=(DESK_HIERARCHY, MKT_BASE_CCY)),
        formula=_formula("book_desk_concentration", "share"),
        replaces_legacy_ids=("book_desk_concentration",)),

    RecipeDefinitionV2(
        recipe_id="benchmark_basis_dislocation", revision=1, family="markets",
        primary_objective=BASIS,
        business_definition=(
            "Z-score of the spread between two DISTINCT benchmark rates against its window "
            "history — the two legs are distinct operands."),
        decision_context="basis dislocation",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="benchmark_basis_dislocation", display_label="Basis dislocation",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="days missing either leg are excluded",
            empty_population_policy="fewer than two observations returns null"),
        operands=(
            OperandSpecV2(role="instrument", concept="instrument_id",
                          operand_class="entity_key",
                          allowed_source_grains=("rate_observation",)),
            OperandSpecV2(role="leg_a", concept="benchmark_rate", operand_class="measure",
                          allowed_source_grains=("rate_observation",),
                          distinct_binding_group="basis_legs"),
            OperandSpecV2(role="leg_b", concept="benchmark_rate", operand_class="measure",
                          allowed_source_grains=("rate_observation",),
                          distinct_binding_group="basis_legs"),
            OperandSpecV2(role="event_ts", concept="event_timestamp",
                          operand_class="event_timestamp",
                          allowed_source_grains=("rate_observation",))),
        source_grain="rate_observation", output_grain="instrument",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="paired observations of two distinct benchmarks",
            excluded="one physical column serving both legs"),
        formula=_formula("benchmark_basis_dislocation", "dispersion"),
        replaces_legacy_ids=("benchmark_basis_dislocation",)),

    RecipeDefinitionV2(
        recipe_id="counterparty_deterioration_ewi", revision=1, family="markets",
        primary_objective=CPTY_EWI,
        business_definition=(
            "Whether the counterparty's governed risk rating worsened between the window "
            "start and the cutoff — a two-read comparison at legal-entity grain."),
        decision_context="counterparty early warning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="counterparty_deterioration_ewi",
            display_label="Counterparty deterioration",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="a missing rating at either end returns null",
            empty_population_policy="no rating history returns null"),
        operands=(_counterparty("rating_snapshot"),
                  OperandSpecV2(role="rating", concept="customer_risk_rating",
                                operand_class="status",
                                allowed_source_grains=("rating_snapshot",)),
                  _as_of("rating_snapshot")),
        source_grain="rating_snapshot", output_grain="counterparty",
        temporal=TemporalSpecV2(
            anchor_kind="as_of", business_effective_role="as_of_date",
            window_basis="trailing", window_unit="days", window_parameter="window",
            cutoff_inclusivity="inclusive",
            snapshot_policy="rating read at the window START and at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="governed ratings effective at each read",
            excluded="ratings read outside their effective period"),
        formula=_formula("counterparty_deterioration_ewi", "flag"),
        replaces_legacy_ids=("counterparty_deterioration_ewi",)),
)
