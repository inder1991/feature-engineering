"""BR-19 — the wealth pack: 7 recipes; the asset-outflow leaf gets its reviewed primaries.

Wealth flows split contribution from withdrawal (a net number hides the churn half); cash drag
and concentration are portfolio facts under the valuation/FX policy; the suitability-review
state and the risk-profile mismatch are governance facts under the suitability policy; and
NOTHING here touches the intentionally-empty client-attrition leaf — that stays declared-future
by the taxonomy owner's ruling.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
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
    policy_input,
    snapshot_window,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

ASSET_OUTFLOW = "wealth.asset_outflow"
CONCENTRATION = "portfolio_risk.concentration"
MANDATE = "asset_management.mandate_compliance"
ENGAGEMENT = "customer.engagement"

WEALTH_VALUATION = "business_calendar:wealth-valuation-fx-policy"
SUITABILITY = "privacy_purpose:wealth-suitability-and-consent"
WEALTH_CCY = "currency_conversion:wealth-base-currency"


def _client(source: str):
    return entity("client", "customer_id", source)


WEALTH_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="wealth_contribution_flow", revision=1, family="wealth",
        primary_objective=ASSET_OUTFLOW,
        business_definition=("Contributions INTO the client's wealth accounts over the "
                             "window, in base currency — the inflow half, never netted "
                             "against withdrawals."),
        decision_context="wealth inflow",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="wealth_contribution_flow", display_label="Contributions",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=WEALTH_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across the client's wealth accounts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(_client("wealth_flow_event"),
                  measure("flow", "fund_flow", "wealth_flow_event"),
                  event_ts("wealth_flow_event")),
        source_grain="wealth_flow_event", output_grain="client",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="settled contributions",
            excluded="withdrawals (the outflow recipe's own population)",
            policy_refs=(WEALTH_CCY,)),
        formula=formula("wealth", "wealth_contribution_flow", "sum")),
    RecipeDefinitionV2(
        recipe_id="wealth_asset_outflow", revision=1, family="wealth",
        primary_objective=ASSET_OUTFLOW,
        business_definition=("Withdrawals and transfers OUT of the client's wealth accounts "
                             "over the window — the outflow half, the attrition signal the "
                             "leaf exists for."),
        decision_context="asset outflow",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="wealth_asset_outflow", display_label="Asset outflow",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=WEALTH_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across the client's wealth accounts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(_client("wealth_flow_event"),
                  measure("flow", "fund_flow", "wealth_flow_event"),
                  status("direction", "debit_credit_indicator", "wealth_flow_event"),
                  event_ts("wealth_flow_event")),
        source_grain="wealth_flow_event", output_grain="client",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="settled withdrawals and outward transfers",
            excluded="contributions; market value movement (not a flow)",
            policy_refs=(WEALTH_CCY,)),
        formula=formula("wealth", "wealth_asset_outflow", "sum")),
    RecipeDefinitionV2(
        recipe_id="cash_drag_share", revision=1, family="wealth",
        primary_objective=ASSET_OUTFLOW,
        business_definition=("Cash as a share of the client's portfolio value at the cutoff "
                             "under the valuation/FX policy — rising cash drag precedes "
                             "outflow."),
        decision_context="pre-outflow cash build-up",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cash_drag_share", display_label="Cash drag",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="positions with no valuation are excluded and surface as a gap",
            empty_population_policy="no portfolio value returns null",
            zero_denominator_policy="zero portfolio value returns null"),
        operands=(_client("portfolio_snapshot"),
                  measure("cash", "monetary_stock", "portfolio_snapshot"),
                  measure("portfolio_value", "nav", "portfolio_snapshot"),
                  as_of("portfolio_snapshot")),
        source_grain="portfolio_snapshot", output_grain="client",
        temporal=snapshot_window("the valuation effective at the cutoff under the policy"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="valued positions under the valuation/FX policy",
            excluded="stale or mixed-policy valuations",
            policy_refs=(WEALTH_VALUATION, WEALTH_CCY)),
        formula=formula("wealth", "cash_drag_share", "share")),
    RecipeDefinitionV2(
        recipe_id="portfolio_concentration_hhi", revision=1, family="wealth",
        primary_objective=CONCENTRATION,
        business_definition=("Concentration (HHI) of the client's portfolio value across "
                             "instruments at the cutoff."),
        decision_context="diversification",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="portfolio_concentration_hhi", display_label="Portfolio concentration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="positions with no valuation are excluded",
            empty_population_policy="no portfolio value returns null",
            zero_denominator_policy="zero portfolio value returns null"),
        operands=(_client("portfolio_snapshot"),
                  dim("instrument", "instrument_id", "portfolio_snapshot"),
                  measure("position_value", "monetary_stock", "portfolio_snapshot"),
                  as_of("portfolio_snapshot")),
        source_grain="portfolio_snapshot", output_grain="client",
        temporal=snapshot_window("the valuation effective at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="valued positions under the valuation policy",
            excluded="stale valuations",
            policy_refs=(WEALTH_VALUATION, WEALTH_CCY)),
        formula=formula("wealth", "portfolio_concentration_hhi", "share")),
    RecipeDefinitionV2(
        recipe_id="risk_profile_mismatch_flag", revision=1, family="wealth",
        primary_objective=MANDATE,
        business_definition=("Whether the portfolio's measured risk exceeds the client's "
                             "recorded risk profile at the cutoff — a suitability fact under "
                             "the governed suitability policy."),
        decision_context="suitability monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="risk_profile_mismatch_flag", display_label="Risk mismatch",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="clients with no recorded profile return null — unknown, "
                              "never assumed suitable",
            empty_population_policy="no profile coverage returns null"),
        operands=(_client("portfolio_snapshot"),
                  measure("portfolio_risk", "customer_risk_rating", "portfolio_snapshot",
                          unit="score"),
                  policy_input("risk_profile", "customer_risk_rating", "portfolio_snapshot",
                               policy=SUITABILITY),
                  as_of("portfolio_snapshot")),
        source_grain="portfolio_snapshot", output_grain="client",
        temporal=snapshot_window("profile and portfolio risk at the same as-of"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="recorded profiles against measured portfolio risk under the "
                     "suitability policy",
            excluded="suitability assumed where no profile exists",
            policy_refs=(SUITABILITY,)),
        formula=formula("wealth", "risk_profile_mismatch_flag", "flag")),
    RecipeDefinitionV2(
        recipe_id="suitability_review_overdue_flag", revision=1, family="wealth",
        primary_objective=MANDATE,
        business_definition=("Whether the client's suitability review is past its due date "
                             "at the cutoff."),
        decision_context="review currency",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="suitability_review_overdue_flag", display_label="Review overdue",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="clients with no review schedule return null",
            empty_population_policy="no review coverage returns null"),
        operands=(_client("review_schedule"),
                  event_ts("review_schedule", role="review_due", concept="due_date")),
        source_grain="review_schedule", output_grain="client",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="review_due",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled reviews and their due dates",
            excluded="reviews assumed current with no schedule",
            policy_refs=(SUITABILITY,)),
        formula=formula("wealth", "suitability_review_overdue_flag", "flag")),
    RecipeDefinitionV2(
        recipe_id="advisor_interaction_recency", revision=1, family="wealth",
        primary_objective=ENGAGEMENT,
        business_definition=("Days since the client's last advisor interaction at the "
                             "cutoff."),
        decision_context="advisor engagement",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="advisor_interaction_recency", display_label="Advisor recency",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="interactions with null timestamps are excluded",
            empty_population_policy="no interactions in the window returns null"),
        operands=(_client("service_event"),
                  dim("advisor", "relationship_manager_id", "service_event"),
                  event_ts("service_event")),
        source_grain="service_event", output_grain="client",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("wealth", "advisor_interaction_recency", "recency")),
)
