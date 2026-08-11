"""BR-15 — the custody pack: 8 legacy templates → 10 atomic V2 recipes.

Structural corrections: trade date, CONTRACTUAL settlement date and ACTUAL settlement date are
three distinct timestamp operands (one binding group per pair that must differ); a fail is
KNOWABLE only after the contractual settlement date — the temporal policy says so; matching is
a stage BEFORE settlement (matching_status, BR-10's concept); the fail family splits count,
value, rate and age into atoms; corporate actions carry the entitlement/election/deadline
lifecycle; and every recipe names its security/account/market grain.
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
    snapshot_window,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

SETTLEMENT_RISK = "securities_services.custody.settlement_failure_risk"
HOLDINGS = "securities_services.custody.holdings_dynamics"
CORP_ACTIONS = "securities_services.custody.corporate_actions"
SEC_LENDING = "securities_services.securities_lending"
FUND_ADMIN = "securities_services.fund_administration"

MARKET_CALENDAR = "business_calendar:settlement-market-calendar"
SSI_AUTHORITY = "active_state:ssi-standing-instruction-authority"
CUSTODY_CCY = "currency_conversion:custody-base-currency"

#: A fail exists only AFTER contractual settlement date — the knowability rule, stated once.
_FAIL_KNOWABILITY = ("a fail is knowable only after the CONTRACTUAL settlement date has "
                     "passed under the market calendar — before it, a pending trade is "
                     "pending, not failing")


def _settle_dates(source: str) -> tuple:
    return (
        event_ts(source, role="contractual_settlement", concept="settlement_date",
                 group="settle_dates"),
        event_ts(source, role="actual_settlement", concept="event_timestamp",
                 group="settle_dates"),
        dim("trade_date", "trade_date", source),
    )


CUSTODY_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="matching_break_rate", revision=1, family="custody",
        primary_objective=SETTLEMENT_RISK,
        business_definition=(
            "Unmatched/mismatched instructions divided by instructions submitted — the "
            "MATCHING stage, before settlement, read from matching_status."),
        decision_context="pre-settlement matching health",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="matching_break_rate", display_label="Matching break rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="instructions with no matching state are excluded",
            empty_population_policy="no instructions returns null",
            zero_denominator_policy="zero instructions returns null"),
        operands=(entity("account", "account_id", "settlement_instruction"),
                  status("matching", "matching_status", "settlement_instruction"),
                  event_ts("settlement_instruction")),
        source_grain="settlement_instruction", output_grain="account",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="submitted instructions with a matching state",
            excluded="settlement outcomes (a matched trade can still fail — a later stage)",
            policy_refs=(SSI_AUTHORITY,)),
        formula=formula("custody", "matching_break_rate", "ratio"),
        replaces_legacy_ids=("matching_break_rate",)),
    RecipeDefinitionV2(
        recipe_id="pre_settlement_aging", revision=1, family="custody",
        primary_objective=SETTLEMENT_RISK,
        business_definition=(
            "Days between trade date and the CONTRACTUAL settlement date per pending trade — "
            "the pre-settlement pipeline's age, never a fail measure."),
        decision_context="pipeline ageing",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="pre_settlement_aging", display_label="Pre-settlement age",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="trades missing either date are excluded",
            empty_population_policy="no pending trades returns null"),
        operands=(entity("account", "account_id", "settlement_instruction"),
                  *_settle_dates("settlement_instruction")),
        source_grain="settlement_instruction", output_grain="account",
        temporal=event_window("contractual_settlement"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="pending trades under the market calendar",
            excluded="settled or failed trades",
            policy_refs=(MARKET_CALENDAR,)),
        formula=formula("custody", "pre_settlement_aging", "recency"),
        replaces_legacy_ids=("pre_settlement_aging",)),

    # ── the fail family: count, value, rate, age — four atoms ───────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="settlement_fail_count", revision=1, family="custody",
        primary_objective=SETTLEMENT_RISK,
        business_definition=("Count of settlement fails in the window. " + _FAIL_KNOWABILITY),
        decision_context="fail volume",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="settlement_fail_count", display_label="Settlement fails",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="instructions with no settlement state are excluded",
            empty_population_policy="no fails returns zero"),
        operands=(entity("account", "account_id", "settlement_instruction"),
                  status("settlement", "settlement_status", "settlement_instruction"),
                  dim("fail_reason", "settlement_fail", "settlement_instruction",
                      required=False),
                  *_settle_dates("settlement_instruction")),
        source_grain="settlement_instruction", output_grain="account",
        temporal=event_window("contractual_settlement"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="instructions past contractual settlement under the market calendar",
            excluded="pending trades counted as fails before their contractual date",
            policy_refs=(MARKET_CALENDAR, SSI_AUTHORITY)),
        formula=formula("custody", "settlement_fail_count", "count"),
        replaces_legacy_ids=("settlement_fail_rate",)),
    RecipeDefinitionV2(
        recipe_id="settlement_fail_value", revision=1, family="custody",
        primary_objective=SETTLEMENT_RISK,
        business_definition=("Value of settlement fails in the window, in base currency. "
                             + _FAIL_KNOWABILITY),
        decision_context="fail value at risk",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="settlement_fail_value", display_label="Fail value",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CUSTODY_CCY,
            null_input_policy="fails with null value are excluded per the source policy",
            empty_population_policy="no fails returns zero",
            aggregation_over_entity="sum across accounts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(entity("account", "account_id", "settlement_instruction"),
                  measure("value", "monetary_flow", "settlement_instruction"),
                  status("settlement", "settlement_status", "settlement_instruction"),
                  *_settle_dates("settlement_instruction")),
        source_grain="settlement_instruction", output_grain="account",
        temporal=event_window("contractual_settlement"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="instructions past contractual settlement",
            excluded="pending trades before their contractual date",
            policy_refs=(MARKET_CALENDAR, CUSTODY_CCY)),
        formula=formula("custody", "settlement_fail_value", "sum"),
        replaces_legacy_ids=("settlement_fail_rate",)),
    RecipeDefinitionV2(
        recipe_id="settlement_fail_rate", revision=1, family="custody",
        primary_objective=SETTLEMENT_RISK,
        business_definition=("Fails divided by instructions due to settle in the window. "
                             + _FAIL_KNOWABILITY),
        decision_context="fail rate",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="settlement_fail_rate", display_label="Fail rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="instructions with no settlement state are excluded",
            empty_population_policy="nothing due returns null",
            zero_denominator_policy="zero due returns null"),
        operands=(entity("account", "account_id", "settlement_instruction"),
                  status("settlement", "settlement_status", "settlement_instruction"),
                  *_settle_dates("settlement_instruction")),
        source_grain="settlement_instruction", output_grain="account",
        temporal=event_window("contractual_settlement"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="instructions past contractual settlement",
            excluded="pending trades before their contractual date",
            policy_refs=(MARKET_CALENDAR,)),
        formula=formula("custody", "settlement_fail_rate", "ratio"),
        replaces_legacy_ids=("settlement_fail_rate",)),
    RecipeDefinitionV2(
        recipe_id="settlement_fail_age_max", revision=1, family="custody",
        primary_objective=SETTLEMENT_RISK,
        business_definition=(
            "Oldest open fail's age in days (actual minus contractual settlement). "
            + _FAIL_KNOWABILITY),
        decision_context="fail ageing",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="settlement_fail_age_max", display_label="Oldest fail age",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="fails missing either date are excluded",
            empty_population_policy="no open fails returns null"),
        operands=(entity("account", "account_id", "settlement_instruction"),
                  status("settlement", "settlement_status", "settlement_instruction"),
                  *_settle_dates("settlement_instruction")),
        source_grain="settlement_instruction", output_grain="account",
        temporal=event_window("contractual_settlement"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="open fails past contractual settlement",
            excluded="settled trades",
            policy_refs=(MARKET_CALENDAR,)),
        formula=formula("custody", "settlement_fail_age_max", "extremum"),
        replaces_legacy_ids=("fail_ageing_buckets",)),

    RecipeDefinitionV2(
        recipe_id="corporate_action_complexity", revision=1, family="custody",
        primary_objective=CORP_ACTIONS,
        business_definition=(
            "Count of ELECTIVE corporate-action events with open response deadlines — the "
            "event carries entitlement, election, deadline and payment lifecycle stages."),
        decision_context="corporate-action workload",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="corporate_action_complexity", display_label="Elective CA events",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="events with unknown kind are excluded",
            empty_population_policy="no events returns zero"),
        operands=(entity("account", "account_id", "corporate_action_event"),
                  dim("action", "corporate_action", "corporate_action_event"),
                  event_ts("corporate_action_event"),
                  event_ts("corporate_action_event", role="response_deadline",
                           concept="due_date")),
        source_grain="corporate_action_event", output_grain="account",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="elective actions with entitlement and deadline stages",
            excluded="mandatory actions with no election",
            policy_refs=(MARKET_CALENDAR,)),
        formula=formula("custody", "corporate_action_complexity", "count"),
        replaces_legacy_ids=("corporate_action_complexity",)),
    RecipeDefinitionV2(
        recipe_id="sec_lending_utilisation", revision=1, family="custody",
        primary_objective=SEC_LENDING,
        business_definition=(
            "Value on loan divided by lendable value at the cutoff, per security."),
        decision_context="lending utilization",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="sec_lending_utilisation", display_label="Lending utilization",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="securities missing either side return null",
            empty_population_policy="no lendable value returns null",
            zero_denominator_policy="zero lendable value returns null"),
        operands=(entity("security", "instrument_id", "lending_snapshot"),
                  measure("on_loan", "securities_loan", "lending_snapshot"),
                  measure("lendable", "custody_holding", "lending_snapshot"),
                  as_of("lending_snapshot")),
        source_grain="lending_snapshot", output_grain="security",
        temporal=snapshot_window("latest-known lending snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="lendable holdings and open loans at the same as-of",
            excluded="mixed-as-of pairs",
            policy_refs=(CUSTODY_CCY,)),
        formula=formula("custody", "sec_lending_utilisation", "ratio"),
        replaces_legacy_ids=("sec_lending_utilisation",)),
    RecipeDefinitionV2(
        recipe_id="nav_strike_timeliness", revision=1, family="custody",
        primary_objective=FUND_ADMIN,
        business_definition=(
            "Share of NAV strikes delivered on time against the valuation calendar."),
        decision_context="fund administration SLA",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="nav_strike_timeliness", display_label="NAV timeliness",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="strikes with no scheduled time are excluded",
            empty_population_policy="no strikes due returns null",
            zero_denominator_policy="zero strikes due returns null"),
        operands=(entity("fund", "fund", "nav_strike_event"),
                  status("nav_state", "nav", "nav_strike_event"),
                  event_ts("nav_strike_event")),
        source_grain="nav_strike_event", output_grain="fund",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled strikes under the valuation calendar",
            excluded="ad-hoc revaluations",
            policy_refs=(MARKET_CALENDAR,)),
        formula=formula("custody", "nav_strike_timeliness", "share"),
        replaces_legacy_ids=("nav_strike_timeliness",)),
    RecipeDefinitionV2(
        recipe_id="custody_holding_dynamics", revision=1, family="custody",
        primary_objective=HOLDINGS,
        business_definition=(
            "OLS slope of custody holding value over the window's snapshots, per account."),
        decision_context="holdings trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="custody_holding_dynamics", display_label="Holdings slope",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="base currency units per day", currency_policy=CUSTODY_CCY,
            null_input_policy="days with no snapshot use the latest prior snapshot",
            empty_population_policy="fewer than two snapshot days returns null"),
        operands=(entity("account", "account_id", "holding_snapshot"),
                  measure("holding_value", "custody_holding", "holding_snapshot"),
                  as_of("holding_snapshot")),
        source_grain="holding_snapshot", output_grain="account",
        temporal=snapshot_window("latest-known holding snapshot at or before each day's "
                                 "cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="holding snapshots in base currency",
            excluded="superseded snapshots",
            policy_refs=(CUSTODY_CCY,)),
        formula=formula("custody", "custody_holding_dynamics", "slope"),
        replaces_legacy_ids=("custody_holding_dynamics",)),
)
