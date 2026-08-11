"""BR-20 — the transaction-banking pack: corporate cash management gets its primaries.

Intraday outputs REQUIRE intraday timestamps (their source grain is the intraday sweep/position
event — end-of-day data structurally cannot compile them, the acceptance held again at account
level); cash-position volatility runs at CLIENT grain over positions; sweep behaviour and
virtual-account utilization are cash-concentration facts. Already existing as atoms: payment
count/value by rail/corridor/purpose (foundation + payments packs), inbound/outbound
concentration (counterparty HHI, fan legs), pool EOD/intraday utilization (corporate pack).
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
    snapshot_window,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

CASH_MGMT = "treasury_alm.cash_management"
CIB_CCY = "currency_conversion:cib-base-currency"


TRANSACTION_BANKING_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="cash_position_volatility", revision=1, family="transaction_banking",
        primary_objective=CASH_MGMT,
        business_definition=("Standard deviation of the client's consolidated end-of-day "
                             "cash position over the window, in base currency."),
        decision_context="cash-position stability",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cash_position_volatility", display_label="Cash-position volatility",
            output_type="numeric", additivity="non_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CIB_CCY,
            null_input_policy="days with no known position use the latest prior position",
            empty_population_policy="fewer than two position days returns null"),
        operands=(entity("client", "customer_id", "cash_position_snapshot"),
                  measure("position", "monetary_stock", "cash_position_snapshot"),
                  as_of("cash_position_snapshot")),
        source_grain="cash_position_snapshot", output_grain="client",
        temporal=snapshot_window("latest consolidated position at each day's cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="consolidated end-of-day positions in base currency",
            excluded="superseded positions",
            policy_refs=(CIB_CCY,)),
        formula=formula("txn_banking", "cash_position_volatility", "dispersion")),
    RecipeDefinitionV2(
        recipe_id="intraday_liquidity_peak_usage", revision=1, family="transaction_banking",
        primary_objective=CASH_MGMT,
        business_definition=("The account's worst INTRADAY liquidity position in the window, "
                             "from intraday position events — an end-of-day snapshot "
                             "structurally cannot see the intraday trough."),
        decision_context="intraday liquidity usage",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="intraday_liquidity_peak_usage", display_label="Intraday peak usage",
            output_type="numeric", additivity="non_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CIB_CCY,
            null_input_policy="events with no intraday timestamp are excluded and surface "
                              "as a gap",
            empty_population_policy="no intraday data returns null — EOD data is not "
                                    "evidence of intraday behaviour"),
        operands=(entity("account", "account_id", "intraday_position_event"),
                  measure("position", "monetary_stock", "intraday_position_event"),
                  event_ts("intraday_position_event")),
        source_grain="intraday_position_event", output_grain="account",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                window_basis="trailing", window_unit="minutes",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="intraday position events with intraday timestamps",
            excluded="end-of-day snapshots standing in for intraday positions",
            policy_refs=(CIB_CCY,)),
        formula=formula("txn_banking", "intraday_liquidity_peak_usage", "extremum")),
    RecipeDefinitionV2(
        recipe_id="pool_sweep_count", revision=1, family="transaction_banking",
        primary_objective=CASH_MGMT,
        business_definition=("Count of sweep executions into/out of the pool structure over "
                             "the window — cash-concentration behaviour as events."),
        decision_context="sweep behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="pool_sweep_count", display_label="Sweep count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="events with unknown pool linkage are excluded",
            empty_population_policy="no sweeps returns zero"),
        operands=(entity("pool", "pooling_structure_id", "intraday_sweep_event"),
                  dim("participant", "account_id", "intraday_sweep_event"),
                  event_ts("intraday_sweep_event")),
        source_grain="intraday_sweep_event", output_grain="pool",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("txn_banking", "pool_sweep_count", "count")),
    RecipeDefinitionV2(
        recipe_id="virtual_account_utilization_share", revision=1,
        family="transaction_banking",
        primary_objective=CASH_MGMT,
        business_definition=("Posted value routed through VIRTUAL accounts as a share of the "
                             "client's total posted value over the window."),
        decision_context="virtual-account adoption",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="virtual_account_utilization_share",
            display_label="Virtual-account share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with unknown account kind are excluded from the "
                              "numerator, kept in the denominator",
            empty_population_policy="no posted value returns null",
            zero_denominator_policy="zero posted value returns null"),
        operands=(entity("client", "customer_id", "transaction"),
                  dim("virtual_account", "virtual_account_id", "transaction",
                      required=False),
                  measure("amount", "monetary_flow", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="client",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted value with account-kind identity",
            excluded="reversed and technical events",
            policy_refs=(CIB_CCY,)),
        formula=formula("txn_banking", "virtual_account_utilization_share", "share")),
)
