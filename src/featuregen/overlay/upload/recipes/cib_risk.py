"""BR-20 — the CIB risk pack: maturity walls, refinancing, FX hedging — methodologies stated.

The maturity wall is CONTRACTUAL-FUTURE anchored (the ladder's facility-side twin); refinancing
concentration is the share of the book maturing inside the horizon; FX exposure and the hedge
ratio read declared exposure and hedge legs — and hedge EFFECTIVENESS stays CONCEPTUAL until
its methodology is a documented governed policy, exactly as the plan orders. Treasury-product
adoption counts products used, with sales outcomes fenced from predictors. Already existing as
atoms: group/obligor exposure, collateral/guarantee coverage, covenant headroom, wrong-way
policy carriage, cross-product stress (corporate + markets packs).
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    LeakageSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes._shared import (
    as_of,
    dim,
    entity,
    event_ts,
    event_window,
    formula,
    measure,
    snapshot_window,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

OBLIGOR = "credit.monitoring.obligor"
EARLY_WARNING = "credit.early_warning"
PORTFOLIO = "markets.market_risk.portfolio"
CLV = "customer.clv"

CIB_CCY = "currency_conversion:cib-base-currency"
HEDGE_LINKAGE = "allocation:hedge-designation-linkage"

_FUTURE_WALL = TemporalSpecV2(
    anchor_kind="contractual_future", business_effective_role="as_of_date",
    window_basis="future_horizon", window_unit="days", window_parameter="window",
    cutoff_inclusivity="exclusive",
    future_horizon_policy="contract terms knowable AT the cutoff: (cutoff, cutoff+window] "
                          "reads contractual facility maturities only")


CIB_RISK_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="facility_maturity_wall", revision=1, family="cib_risk",
        primary_objective=OBLIGOR, supporting_objectives=(EARLY_WARNING,),
        business_definition=("Facility exposure contractually maturing in the forward bucket "
                             "(cutoff, cutoff+window] — the refinancing wall, from contract "
                             "terms knowable at the cutoff."),
        decision_context="maturity wall",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="facility_maturity_wall", display_label="Maturity wall",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CIB_CCY,
            null_input_policy="facilities with no contractual maturity are excluded and "
                              "surface as a gap",
            empty_population_policy="an empty bucket returns zero",
            aggregation_over_entity="sum within the bucket; a facility appears in exactly "
                                    "one bucket per as-of",
            aggregation_over_time="never summed across as-ofs"),
        operands=(entity("facility", "facility_id", "facility_day_snapshot"),
                  measure("exposure", "ead", "facility_day_snapshot",
                          economic_role="drawn_credit_exposure"),
                  dim("contractual_maturity", "maturity_date", "facility_day_snapshot"),
                  as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="obligor",
        temporal=_FUTURE_WALL,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="facilities with contractual maturities knowable at the cutoff",
            excluded="behavioural refinancing assumptions",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib_risk", "facility_maturity_wall", "sum")),
    RecipeDefinitionV2(
        recipe_id="refinancing_concentration_share", revision=1, family="cib_risk",
        primary_objective=EARLY_WARNING,
        business_definition=("The maturity wall as a share of total facility exposure — how "
                             "much of the book refinances inside the horizon."),
        decision_context="refinancing concentration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="refinancing_concentration_share",
            display_label="Refinancing concentration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="facilities with no contractual maturity are excluded from "
                              "the numerator, kept in the denominator",
            empty_population_policy="no facility exposure returns null",
            zero_denominator_policy="zero exposure returns null"),
        operands=(entity("obligor", "obligor_id", "facility_day_snapshot"),
                  measure("exposure", "ead", "facility_day_snapshot",
                          economic_role="drawn_credit_exposure"),
                  dim("contractual_maturity", "maturity_date", "facility_day_snapshot"),
                  as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="obligor",
        temporal=_FUTURE_WALL,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="contractual maturities against total exposure at one as-of",
            excluded="behavioural assumptions",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib_risk", "refinancing_concentration_share", "share")),
    RecipeDefinitionV2(
        recipe_id="fx_net_exposure", revision=1, family="cib_risk",
        primary_objective=PORTFOLIO,
        business_definition=("The client's net FX exposure per currency at the cutoff, in "
                             "base currency under the governed conversion."),
        decision_context="FX exposure",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="fx_net_exposure", display_label="Net FX exposure",
            output_type="numeric", additivity="semi_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CIB_CCY,
            null_input_policy="positions with no currency identity are excluded and "
                              "surface as a gap",
            empty_population_policy="no FX positions returns zero",
            aggregation_over_entity="net within a currency; never across currencies",
            aggregation_over_time="latest snapshot only"),
        operands=(entity("client", "customer_id", "position_snapshot"),
                  dim("currency", "currency_code", "position_snapshot"),
                  measure("position", "monetary_stock", "position_snapshot"),
                  as_of("position_snapshot")),
        source_grain="position_snapshot", output_grain="client",
        temporal=snapshot_window("latest position snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="positions with currency identity at one as-of",
            excluded="cross-currency netting without conversion",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib_risk", "fx_net_exposure", "snapshot")),
    RecipeDefinitionV2(
        recipe_id="fx_hedge_ratio", revision=1, family="cib_risk",
        primary_objective=PORTFOLIO,
        business_definition=("Hedged notional divided by net FX exposure per currency, both "
                             "sides at the same as-of, hedge legs linked through the "
                             "governed hedge-designation policy."),
        decision_context="hedge coverage",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="fx_hedge_ratio", display_label="Hedge ratio",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="hedges with no designation linkage are excluded and surface "
                              "as a gap",
            empty_population_policy="no FX exposure returns null",
            zero_denominator_policy="zero exposure returns null"),
        operands=(entity("client", "customer_id", "position_snapshot"),
                  dim("currency", "currency_code", "position_snapshot"),
                  measure("exposure", "monetary_stock", "position_snapshot"),
                  measure("hedged", "notional", "position_snapshot"),
                  as_of("position_snapshot")),
        source_grain="position_snapshot", output_grain="client",
        temporal=snapshot_window("exposure and designated hedges at the same as-of"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="designated hedges against exposure under the linkage policy",
            excluded="undesignated derivatives counted as hedges",
            policy_refs=(HEDGE_LINKAGE, CIB_CCY)),
        formula=formula("cib_risk", "fx_hedge_ratio", "ratio")),
    RecipeDefinitionV2(
        recipe_id="hedge_effectiveness_pattern", revision=1, family="cib_risk",
        primary_objective=PORTFOLIO,
        business_definition=("How effectively the designated hedges offset the exposure's "
                             "value changes."),
        decision_context="hedge effectiveness",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "Effectiveness requires a DOCUMENTED methodology (dollar-offset, regression, "
            "critical terms) as governed policy — the plan keeps it blocked until then; an "
            "effectiveness number without its methodology is unauditable."),
        output=OutputSpecV2(
            output_id="hedge_effectiveness_pattern", display_label="Hedge effectiveness",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern",
            zero_denominator_policy="not applicable — conceptual pattern"),
        operands=(entity("client", "customer_id", "position_snapshot"),),
        source_grain="position_snapshot", output_grain="client",
        temporal=event_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,)),
    RecipeDefinitionV2(
        recipe_id="treasury_product_adoption_count", revision=1, family="cib_risk",
        primary_objective=CLV,
        business_definition=("Distinct treasury products the client used over the window — "
                             "adoption as usage facts; SALES outcomes are fenced from "
                             "predictors by the leakage declaration."),
        decision_context="treasury adoption",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="treasury_product_adoption_count",
            display_label="Treasury products used",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown product are excluded",
            empty_population_policy="no treasury activity returns zero"),
        operands=(entity("client", "customer_id", "transaction"),
                  dim("product", "product_id", "transaction"),
                  dim("txn_type", "transaction_type", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="client",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=LeakageSpecV2(
            classification="near_label",
            permitted_stages=("monitoring", "relationship_planning"),
            prohibited_stages=("sales_outcome_prediction",)),
        formula=formula("cib_risk", "treasury_product_adoption_count", "distinct_count")),
)
