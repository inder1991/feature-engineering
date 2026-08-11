"""BR-20 — the trade-finance pack: lifecycle-first, with the stage as an identity-bearing choice.

The LC/guarantee lifecycle-event count carries its STAGE as a semantic parameter (issuance,
amendment, utilization, expiry, claim — five different features by identity, one recipe body);
documentary discrepancies and processing durations are document-lifecycle facts; the SCF
approved-to-paid duration subtracts two NAMED stage timestamps; contingent-to-funded conversion
reads both exposure roles. Already existing as atoms: rollover counts, DSO, dilution, SCF
program utilization, the working-capital cycle (corporate pack).
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    OutputSpecV2,
    ParameterSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes._shared import (
    dim,
    entity,
    event_ts,
    event_window,
    formula,
    measure,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

TRADE_FINANCE = "corporate_trade.trade_finance"
SCF = "corporate_trade.supply_chain_finance"
WORKING_CAPITAL = "corporate_trade.working_capital"

CIB_CCY = "currency_conversion:cib-base-currency"

_LIFECYCLE_STAGE = ParameterSpecV2(
    name="lifecycle_stage", parameter_class="semantic",
    allowed_values=("issuance", "amendment", "utilization", "expiry", "claim"),
    identity_projection="stage={value}",
    display_projection="{value} events")


TRADE_FINANCE_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="lc_lifecycle_event_count", revision=1, family="trade_finance",
        primary_objective=TRADE_FINANCE,
        business_definition=("Count of the chosen LC/guarantee lifecycle stage's events over "
                             "the window — issuance, amendment, utilization, expiry and "
                             "claim are five features by identity, one body."),
        decision_context="instrument lifecycle volume",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="lc_lifecycle_event_count", display_label="LC lifecycle events",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="events with unknown lifecycle kind are excluded and surface "
                              "as a gap",
            empty_population_policy="no instrument events returns zero"),
        operands=(entity("instrument", "instrument_id", "trade_instrument_event"),
                  dim("instrument_type", "instrument_type", "trade_instrument_event"),
                  status("lifecycle_event", "lc_guarantee_event", "trade_instrument_event"),
                  event_ts("trade_instrument_event")),
        source_grain="trade_instrument_event", output_grain="instrument",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW, _LIFECYCLE_STAGE),
        eligibility=EligibilitySpecV2(
            included="instrument lifecycle events of the chosen stage",
            excluded="generic exposure totals with no lifecycle"),
        formula=formula("trade", "lc_lifecycle_event_count", "count")),
    RecipeDefinitionV2(
        recipe_id="documentary_discrepancy_rate", revision=1, family="trade_finance",
        primary_objective=TRADE_FINANCE,
        business_definition=("Document presentations with discrepancies divided by "
                             "presentations examined over the window."),
        decision_context="documentary quality",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="documentary_discrepancy_rate", display_label="Discrepancy rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="presentations with no examination outcome are excluded",
            empty_population_policy="no presentations returns null",
            zero_denominator_policy="zero presentations returns null"),
        operands=(entity("instrument", "instrument_id", "document_presentation"),
                  status("examination", "lifecycle_state", "document_presentation"),
                  event_ts("document_presentation")),
        source_grain="document_presentation", output_grain="instrument",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("trade", "documentary_discrepancy_rate", "ratio")),
    RecipeDefinitionV2(
        recipe_id="document_processing_days", revision=1, family="trade_finance",
        primary_objective=TRADE_FINANCE,
        business_definition=("Average days from document presentation to examination "
                             "decision — two NAMED stage timestamps subtracted."),
        decision_context="documentary throughput",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="document_processing_days", display_label="Processing days",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="presentations missing either timestamp are excluded",
            empty_population_policy="no decisions in the window returns null"),
        operands=(entity("instrument", "instrument_id", "document_presentation"),
                  event_ts("document_presentation", role="presented_ts",
                           group="doc_times"),
                  event_ts("document_presentation", role="decided_ts", group="doc_times")),
        source_grain="document_presentation", output_grain="instrument",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="decided_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("trade", "document_processing_days", "ratio")),
    RecipeDefinitionV2(
        recipe_id="scf_approved_to_paid_days", revision=1, family="trade_finance",
        primary_objective=SCF,
        business_definition=("Average days from invoice APPROVAL to supplier PAYMENT in the "
                             "SCF program — the funding latency, two named stages."),
        decision_context="SCF throughput",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="scf_approved_to_paid_days", display_label="Approved-to-paid days",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="fundings missing either timestamp are excluded",
            empty_population_policy="no payments in the window returns null"),
        operands=(entity("program", "pooling_structure_id", "scf_funding_event"),
                  dim("invoice", "invoice_id", "scf_funding_event"),
                  event_ts("scf_funding_event", role="approved_ts", group="scf_times"),
                  event_ts("scf_funding_event", role="paid_ts", group="scf_times")),
        source_grain="scf_funding_event", output_grain="pool",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="paid_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("trade", "scf_approved_to_paid_days", "ratio")),
    RecipeDefinitionV2(
        recipe_id="trade_flow_trend", revision=1, family="trade_finance",
        primary_objective=TRADE_FINANCE,
        business_definition=("OLS slope of the obligor's trade-transaction value over the "
                             "window — trade-flow decline as its own atomic signal."),
        decision_context="trade-flow trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="trade_flow_trend", display_label="Trade-flow trend",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="base currency units per day", currency_policy=CIB_CCY,
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="fewer than two active days returns null"),
        operands=(entity("obligor", "obligor_id", "transaction"),
                  measure("amount", "monetary_flow", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="obligor",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted trade-finance transactions",
            excluded="reversed and technical events",
            policy_refs=(CIB_CCY,)),
        formula=formula("trade", "trade_flow_trend", "slope")),
    RecipeDefinitionV2(
        recipe_id="contingent_to_funded_share", revision=1, family="trade_finance",
        primary_objective=WORKING_CAPITAL,
        business_definition=("Contingent exposure converted to funded (drawn) exposure over "
                             "the window, as a share of opening contingent — both exposure "
                             "roles declared, the conversion the ladder's own fact."),
        decision_context="contingent conversion",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="contingent_to_funded_share", display_label="Contingent conversion",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="facilities missing either exposure are excluded and surface "
                              "as a gap",
            empty_population_policy="no opening contingent returns null",
            zero_denominator_policy="zero opening contingent returns null"),
        operands=(entity("facility", "facility_id", "facility_day_snapshot"),
                  measure("contingent", "contingent_exposure", "facility_day_snapshot"),
                  measure("drawn", "drawn_principal", "facility_day_snapshot",
                          economic_role="drawn_credit_exposure"),
                  event_ts("facility_day_snapshot", role="as_of_date",
                           concept="as_of_date")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=TemporalSpecV2(
            anchor_kind="as_of", business_effective_role="as_of_date",
            window_basis="trailing", window_unit="days", window_parameter="window",
            cutoff_inclusivity="inclusive",
            snapshot_policy="exposures read at the window START and at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="both exposure roles at the two reads",
            excluded="conversion inferred from one side alone",
            policy_refs=(CIB_CCY,)),
        formula=formula("trade", "contingent_to_funded_share", "share")),
)
