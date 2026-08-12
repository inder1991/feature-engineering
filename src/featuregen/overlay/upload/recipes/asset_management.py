"""BR-15 — the asset-management pack: 8 legacy templates → 9 atomic V2 recipes.

Structural corrections: fund/portfolio/share-class grain named per recipe; NAV reads carry the
valuation calendar and NAV VERSION policy; market performance and investor flows are SEPARATE
recipes (an AUM move is price + flow, never one number); benchmark identity/methodology and
fee basis are governed policies; redemption coverage reads liquidity buckets, redemption terms
and gates; historical net flow stays deterministic — redemption-RISK is a model's job and
arrives as a ModelFeatureSpec when one registers.
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
    event_ts,
    event_window,
    formula,
    measure,
    policy_input,
    snapshot_window,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

FLOWS = "asset_management.redemption.fund_flows"
LIQUIDITY = "asset_management.redemption.fund_liquidity"
AUM = "asset_management.redemption.aum_stability"
MANDATE = "asset_management.mandate_compliance"
PERFORMANCE = "asset_management.performance"

NAV_VERSION = "business_calendar:nav-version-and-valuation-calendar"
BENCHMARK_ID = "risk_corridor:benchmark-identity-methodology"
FEE_BASIS = "threshold:fund-fee-basis"
REDEMPTION_TERMS = "active_state:redemption-terms-gates-swing"
AM_CCY = "currency_conversion:fund-fx-policy"


def _fund(source: str) -> tuple:
    return (entity("fund", "fund", source),
            dim("share_class", "share_class", source, required=False))


AM_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="net_fund_flow", revision=1, family="asset_management",
        primary_objective=FLOWS,
        business_definition=(
            "Subscriptions minus redemptions over the window, per fund/share-class — "
            "INVESTOR flows only, never the market's price contribution."),
        decision_context="flow history (deterministic; redemption RISK is a model)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="net_fund_flow", display_label="Net fund flow",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="fund base currency", currency_policy=AM_CCY,
            null_input_policy="orders with null value are excluded per the source policy",
            empty_population_policy="no orders returns zero",
            aggregation_over_entity="sum within the fund/share-class",
            aggregation_over_time="sum over disjoint windows"),
        operands=(*_fund("fund_flow_event"),
                  measure("flow", "fund_flow", "fund_flow_event"),
                  event_ts("fund_flow_event")),
        source_grain="fund_flow_event", output_grain="fund",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="settled subscriptions and redemptions",
            excluded="market/price movement (aum decomposition owns it)",
            policy_refs=(AM_CCY,)),
        formula=formula("am", "net_fund_flow", "sum"),
        replaces_legacy_ids=("net_fund_flow_trend",)),
    RecipeDefinitionV2(
        recipe_id="performance_vs_benchmark", revision=1, family="asset_management",
        primary_objective=PERFORMANCE,
        business_definition=(
            "Fund return minus benchmark return over the window — the benchmark's identity "
            "and methodology are governed policy, and both legs read versioned NAVs under "
            "the valuation calendar."),
        decision_context="relative performance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="performance_vs_benchmark", display_label="Excess return",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            null_input_policy="days missing either NAV or benchmark level are excluded",
            empty_population_policy="fewer than two valuation days returns null"),
        operands=(*_fund("nav_observation"),
                  measure("nav_level", "nav", "nav_observation"),
                  policy_input("benchmark", "benchmark", "nav_observation",
                               policy=BENCHMARK_ID),
                  as_of("nav_observation")),
        source_grain="nav_observation", output_grain="fund",
        temporal=snapshot_window("versioned NAV under the valuation calendar; the FINAL "
                                 "version at each date, never a superseded strike"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="final-version NAVs and the governed benchmark's levels",
            excluded="superseded NAV versions; benchmark substitution",
            policy_refs=(NAV_VERSION, BENCHMARK_ID, AM_CCY)),
        formula=formula("am", "performance_vs_benchmark", "ratio"),
        replaces_legacy_ids=("performance_vs_benchmark",)),
    RecipeDefinitionV2(
        recipe_id="share_class_flow_mix", revision=1, family="asset_management",
        primary_objective=FLOWS,
        business_definition=(
            "One share-class's net flow as a share of the fund's total net flow."),
        decision_context="flow composition",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="share_class_flow_mix", display_label="Share-class flow mix",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="orders with no share-class are excluded",
            empty_population_policy="no fund flow returns null",
            zero_denominator_policy="zero fund flow returns null"),
        operands=(entity("fund", "fund", "fund_flow_event"),
                  dim("share_class", "share_class", "fund_flow_event"),
                  measure("flow", "fund_flow", "fund_flow_event"),
                  event_ts("fund_flow_event")),
        source_grain="fund_flow_event", output_grain="share_class",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="settled flows with share-class identity",
            excluded="fund-level rows with no class",
            policy_refs=(AM_CCY,)),
        formula=formula("am", "share_class_flow_mix", "share"),
        replaces_legacy_ids=("share_class_flow_mix",)),
    RecipeDefinitionV2(
        recipe_id="redemption_liquidity_coverage", revision=1, family="asset_management",
        primary_objective=LIQUIDITY,
        business_definition=(
            "Liquid-bucket assets divided by stressed redemptions under the fund's "
            "redemption terms, gates and swing-pricing policy."),
        decision_context="redemption liquidity",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="redemption_liquidity_coverage", display_label="Liquidity coverage",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="assets with no liquidity bucket are excluded and surface as "
                              "a data gap",
            empty_population_policy="no redemption stress returns null",
            zero_denominator_policy="zero stressed redemptions returns null"),
        operands=(entity("fund", "fund", "liquidity_snapshot"),
                  measure("liquid_assets", "hqla", "liquidity_snapshot"),
                  measure("stressed_redemptions", "fund_flow", "liquidity_snapshot"),
                  policy_input("terms", "mandate", "liquidity_snapshot",
                               policy=REDEMPTION_TERMS),
                  as_of("liquidity_snapshot")),
        source_grain="liquidity_snapshot", output_grain="fund",
        temporal=snapshot_window("latest liquidity snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="bucketed assets and stressed redemptions under the redemption terms",
            excluded="unbucketed assets assumed liquid",
            policy_refs=(REDEMPTION_TERMS, AM_CCY)),
        formula=formula("am", "redemption_liquidity_coverage", "ratio"),
        replaces_legacy_ids=("redemption_liquidity_coverage",)),
    RecipeDefinitionV2(
        recipe_id="aum_stability", revision=1, family="asset_management",
        primary_objective=AUM,
        business_definition=(
            "OLS slope of fund AUM over the window's versioned valuations — the level's "
            "trajectory; its decomposition into flow vs market is the flow recipes' job."),
        decision_context="AUM trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="aum_stability", display_label="AUM slope",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="fund base currency per day", currency_policy=AM_CCY,
            null_input_policy="days with no final NAV version are excluded",
            empty_population_policy="fewer than two valuation days returns null"),
        operands=(entity("fund", "fund", "nav_observation"),
                  measure("aum", "monetary_stock", "nav_observation"),
                  as_of("nav_observation")),
        source_grain="nav_observation", output_grain="fund",
        temporal=snapshot_window("final NAV version at each valuation date"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="final-version valuations",
            excluded="superseded NAV versions",
            policy_refs=(NAV_VERSION, AM_CCY)),
        formula=formula("am", "aum_stability", "slope"),
        replaces_legacy_ids=("aum_stability",)),
    RecipeDefinitionV2(
        recipe_id="tracking_error_breach_proximity", revision=1, family="asset_management",
        primary_objective=MANDATE,
        business_definition=(
            "Tracking error against the mandate's limit under the governed benchmark "
            "identity — proximity to breach, at the same as-of."),
        decision_context="mandate compliance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="tracking_error_breach_proximity", display_label="TE breach proximity",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="funds missing either side at the as-of return null",
            empty_population_policy="no mandate limit returns null",
            zero_denominator_policy="a zero limit returns null"),
        operands=(entity("fund", "fund", "risk_observation"),
                  measure("tracking_error", "tracking_error", "risk_observation",
                          unit="rate"),
                  policy_input("mandate_limit", "mandate", "risk_observation",
                               policy=BENCHMARK_ID),
                  as_of("risk_observation")),
        source_grain="risk_observation", output_grain="fund",
        temporal=snapshot_window("latest risk observation at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="tracking error against the mandated benchmark",
            excluded="benchmark substitution",
            policy_refs=(BENCHMARK_ID,)),
        formula=formula("am", "tracking_error_breach_proximity", "ratio"),
        replaces_legacy_ids=("tracking_error_breach_proximity",)),
    RecipeDefinitionV2(
        recipe_id="mandate_breach_proximity", revision=1, family="asset_management",
        primary_objective=MANDATE,
        business_definition=(
            "Portfolio exposure against a mandate constraint at the same as-of — proximity "
            "to the nearest declared limit."),
        decision_context="mandate compliance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="mandate_breach_proximity", display_label="Mandate breach proximity",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="constraints missing either side return null",
            empty_population_policy="no declared constraints returns null",
            zero_denominator_policy="a zero constraint returns null"),
        operands=(entity("portfolio", "portfolio_id", "risk_observation"),
                  measure("exposure", "monetary_stock", "risk_observation"),
                  policy_input("constraint", "mandate", "risk_observation",
                               policy=REDEMPTION_TERMS),
                  as_of("risk_observation")),
        source_grain="risk_observation", output_grain="portfolio",
        temporal=snapshot_window("latest observation at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="exposures against declared mandate constraints",
            excluded="undeclared constraints",
            policy_refs=(REDEMPTION_TERMS,)),
        formula=formula("am", "mandate_breach_proximity", "ratio"),
        replaces_legacy_ids=("mandate_breach_proximity",)),
    RecipeDefinitionV2(
        recipe_id="expense_ratio_competitiveness", revision=1, family="asset_management",
        primary_objective=PERFORMANCE,
        business_definition=(
            "The share-class expense ratio against its peer-group median, under the "
            "governed fee basis."),
        decision_context="fee competitiveness",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="expense_ratio_competitiveness", display_label="Fee competitiveness",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="classes with no peer group return null",
            empty_population_policy="an empty peer group returns null",
            zero_denominator_policy="a zero peer median returns null"),
        operands=(entity("share_class", "share_class", "fee_observation"),
                  measure("expense_ratio", "expense_ratio", "fee_observation", unit="rate"),
                  dim("peer_group", "peer_group", "fee_observation"),
                  as_of("fee_observation")),
        source_grain="fee_observation", output_grain="share_class",
        temporal=snapshot_window("latest fee observation at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="expense ratios under the governed fee basis",
            excluded="fee rows with no declared basis",
            policy_refs=(FEE_BASIS,)),
        formula=formula("am", "expense_ratio_competitiveness", "ratio"),
        replaces_legacy_ids=("expense_ratio_competitiveness",)),
)
