"""BR-16 — the Corporate/CIB pack: 12 legacy templates → 15 atomic V2 recipes.

Structural corrections: LC/guarantee outputs read the INSTRUMENT lifecycle (identity, type,
issue/amend/utilize/expire/claim/rollover events — BR-10's lc_guarantee_event), never generic
contingent exposure alone; DSO, dilution and debtor concentration require the INVOICE lifecycle
(identity, issue/due/paid dates, credit notes, debtor identity); SCF reads buyer, supplier,
program and program limit; group exposure respects the EFFECTIVE-DATED legal hierarchy and the
intra-group elimination policy; guarantees carry enforceability, expiry, guarantor quality and
the wrong-way-risk policy; the working-capital cycle is the BR-6 signed-sum exemplar's shape
over declared AR/AP/inventory periods; cash pooling splits END-OF-DAY utilization from the
INTRADAY peak — the peak's source grain is the intraday sweep event, so a daily snapshot
structurally cannot compile it; cross-product stress splits stressed-line count from exposure
trend; and obligor_facility_count stays atomic and honestly FORMULA_AUTHORABLE on its reviewed
Formula-v1 expectation.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
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

TRADE_FINANCE = "corporate_trade.trade_finance"
SCF = "corporate_trade.supply_chain_finance"
WORKING_CAPITAL = "corporate_trade.working_capital"
RECEIVABLES = "corporate_trade.receivables_finance"
OBLIGOR = "credit.monitoring.obligor"
EARLY_WARNING = "credit.early_warning"
CONCENTRATION = "portfolio_risk.concentration"

GROUP_ELIMINATION = "allocation:intra-group-elimination"
LEGAL_HIERARCHY = "active_state:effective-dated-legal-hierarchy"
WRONG_WAY = "risk_corridor:guarantee-wrong-way-correlation"
STRESS_THRESHOLD = "threshold:cross-product-stress"
CIB_CCY = "currency_conversion:cib-base-currency"


CORPORATE_CIB_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="facility_utilisation_headroom", revision=1, family="corporate_cib",
        primary_objective=OBLIGOR, supporting_objectives=(EARLY_WARNING,),
        business_definition=(
            "Available headroom (approved limit minus drawn) as a share of the limit, per "
            "facility, both sides at the same as-of."),
        decision_context="facility headroom",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="facility_utilisation_headroom", display_label="Facility headroom",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="facilities missing either side return null",
            empty_population_policy="no limit record returns null",
            zero_denominator_policy="a zero limit returns null"),
        operands=(entity("facility", "facility_id", "facility_day_snapshot"),
                  measure("headroom", "available_limit", "facility_day_snapshot",
                          economic_role="undrawn_headroom"),
                  measure("limit", "limit", "facility_day_snapshot",
                          economic_role="approved_credit_limit"),
                  as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=snapshot_window("latest facility snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="facility snapshots with both sides at one as-of",
            excluded="mixed-as-of pairs",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib", "facility_utilisation_headroom", "ratio"),
        replaces_legacy_ids=("facility_utilisation_headroom",)),

    # ── LC/guarantee reads the INSTRUMENT lifecycle (corrections 1-2) ───────────────────────────
    RecipeDefinitionV2(
        recipe_id="lc_guarantee_rollover_count", revision=1, family="corporate_cib",
        primary_objective=TRADE_FINANCE,
        business_definition=(
            "Count of TRUE rollover events on the instrument in the window — the lifecycle "
            "event kind distinguishes a rollover from an amendment-and-extension, and the "
            "instrument's identity and type are operands; generic contingent exposure alone "
            "can never produce this."),
        decision_context="trade-finance renewal behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="lc_guarantee_rollover_count", display_label="LC/guarantee rollovers",
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
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="instrument lifecycle events (issue/amend/utilize/expire/claim/rollover)",
            excluded="amendments and extensions counted as rollovers; contingent exposure "
                     "totals with no lifecycle"),
        formula=formula("cib", "lc_guarantee_rollover_count", "count"),
        replaces_legacy_ids=("lc_guarantee_rollover",)),

    # ── invoice lifecycle: DSO and dilution (correction 3) ──────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="invoice_dso_days", revision=1, family="corporate_cib",
        primary_objective=RECEIVABLES,
        business_definition=(
            "Average days from invoice ISSUE to PAYMENT for invoices paid in the window — "
            "the invoice's identity, issue date, due date, paid date and debtor identity are "
            "operands; a payment flow without an invoice lifecycle cannot produce DSO."),
        decision_context="receivables performance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="invoice_dso_days", display_label="DSO (days)",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="invoices missing issue or paid date are excluded and surface "
                              "as a gap",
            empty_population_policy="no invoices paid in the window returns null"),
        operands=(entity("debtor", "customer_id", "invoice_lifecycle"),
                  dim("invoice", "invoice_id", "invoice_lifecycle"),
                  status("invoice_state", "invoice_status", "invoice_lifecycle"),
                  event_ts("invoice_lifecycle", role="issue_ts", group="invoice_dates"),
                  event_ts("invoice_lifecycle", role="paid_ts", group="invoice_dates"),
                  dim("due_date", "due_date", "invoice_lifecycle")),
        source_grain="invoice_lifecycle", output_grain="debtor",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="paid_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="invoices with full lifecycle dates and debtor identity",
            excluded="payment flows with no invoice linkage"),
        formula=formula("cib", "invoice_dso_days", "ratio"),
        replaces_legacy_ids=("invoice_finance_dynamics",)),
    RecipeDefinitionV2(
        recipe_id="invoice_dilution_share", revision=1, family="corporate_cib",
        primary_objective=RECEIVABLES,
        business_definition=(
            "Credit-noted/diluted invoice value as a share of issued value in the window — "
            "an invoice partially paid and later credited counts its credited portion here, "
            "not in DSO."),
        decision_context="receivables dilution risk",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="invoice_dilution_share", display_label="Dilution share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="invoices with unknown state are excluded",
            empty_population_policy="no issued value returns null",
            zero_denominator_policy="zero issued value returns null"),
        operands=(entity("debtor", "customer_id", "invoice_lifecycle"),
                  dim("invoice", "invoice_id", "invoice_lifecycle"),
                  status("invoice_state", "invoice_status", "invoice_lifecycle"),
                  measure("invoice_value", "monetary_flow", "invoice_lifecycle"),
                  event_ts("invoice_lifecycle")),
        source_grain="invoice_lifecycle", output_grain="debtor",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="issued invoices and their credit-note events",
            excluded="dilution inferred without credit-note lifecycle",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib", "invoice_dilution_share", "share"),
        replaces_legacy_ids=("invoice_finance_dynamics",)),

    # ── SCF program (correction 4) ──────────────────────────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="scf_program_utilisation", revision=1, family="corporate_cib",
        primary_objective=SCF,
        business_definition=(
            "Approved-invoice funding outstanding as a share of the SCF program limit — "
            "buyer, supplier, program and the program's limit are operands, so a supplier "
            "changing programs re-binds rather than blends."),
        decision_context="SCF program health",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="scf_program_utilisation", display_label="SCF program utilization",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="fundings with no program linkage are excluded and surface as "
                              "a gap",
            empty_population_policy="no program limit returns null",
            zero_denominator_policy="a zero program limit returns null"),
        operands=(entity("program", "pooling_structure_id", "scf_funding_snapshot"),
                  dim("buyer", "customer_id", "scf_funding_snapshot", group="scf_parties"),
                  dim("supplier", "customer_id", "scf_funding_snapshot",
                      group="scf_parties"),
                  measure("funded", "monetary_stock", "scf_funding_snapshot"),
                  policy_input("program_limit", "limit", "scf_funding_snapshot",
                               policy="threshold:scf-program-limit"),
                  as_of("scf_funding_snapshot")),
        source_grain="scf_funding_snapshot", output_grain="program",
        temporal=snapshot_window("latest funding snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="approved-invoice fundings inside one program",
            excluded="cross-program blending when a supplier moves",
            policy_refs=("threshold:scf-program-limit", CIB_CCY)),
        formula=formula("cib", "scf_program_utilisation", "ratio"),
        replaces_legacy_ids=("supply_chain_finance_dynamics",)),

    RecipeDefinitionV2(
        recipe_id="covenant_breach_count", revision=1, family="corporate_cib",
        primary_objective=EARLY_WARNING,
        business_definition=(
            "Covenant tests BREACHED in the window, waiver and cure state declared — a "
            "waived breach is a waived breach, never a pass."),
        decision_context="covenant monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="covenant_breach_count", display_label="Covenant breaches",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="tests missing threshold or direction are excluded and "
                              "surface as a gap",
            empty_population_policy="no tests in the window returns null"),
        operands=(entity("facility", "facility_id", "covenant_test"),
                  measure("actual", "covenant", "covenant_test", unit="rate"),
                  policy_input("threshold", "covenant", "covenant_test",
                               policy="threshold:covenant-terms"),
                  status("waiver_state", "lifecycle_state", "covenant_test"),
                  event_ts("covenant_test")),
        source_grain="covenant_test", output_grain="facility",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="tests with declared terms and waiver state",
            excluded="waived breaches counted as passes",
            policy_refs=("threshold:covenant-terms",)),
        formula=formula("cib", "covenant_breach_count", "count"),
        replaces_legacy_ids=("covenant_headroom_breach",)),
    RecipeDefinitionV2(
        recipe_id="syndication_concentration", revision=1, family="corporate_cib",
        primary_objective=CONCENTRATION,
        business_definition=(
            "Concentration (HHI) of the bank's syndication shares across deals."),
        decision_context="syndication concentration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="syndication_concentration", display_label="Syndication concentration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="deals with no share are excluded",
            empty_population_policy="no syndicated exposure returns null",
            zero_denominator_policy="zero exposure returns null"),
        operands=(entity("portfolio", "portfolio_id", "syndication_snapshot"),
                  dim("deal", "facility_id", "syndication_snapshot"),
                  measure("share_held", "syndication_share", "syndication_snapshot"),
                  as_of("syndication_snapshot")),
        source_grain="syndication_snapshot", output_grain="portfolio",
        temporal=snapshot_window("latest syndication snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="held syndication shares",
            excluded="agent-only roles with no held share",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib", "syndication_concentration", "share"),
        replaces_legacy_ids=("syndication_concentration",)),

    # ── group exposure over the EFFECTIVE legal hierarchy (corrections 5-6) ─────────────────────
    RecipeDefinitionV2(
        recipe_id="group_exposure_aggregation", revision=1, family="corporate_cib",
        primary_objective=OBLIGOR,
        business_definition=(
            "Group exposure over the EFFECTIVE-DATED legal hierarchy under the intra-group "
            "elimination policy — a subsidiary leaving the group mid-window leaves the "
            "aggregate at that date, and intra-group facilities eliminate rather than "
            "double-count."),
        decision_context="legal-group exposure",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="group_exposure_aggregation", display_label="Group exposure",
            output_type="numeric", additivity="semi_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CIB_CCY,
            null_input_policy="members with unknown hierarchy membership are excluded and "
                              "surface as a gap",
            empty_population_policy="no members returns zero",
            aggregation_over_entity="sum across members AFTER elimination, membership "
                                    "effective at the as-of",
            aggregation_over_time="latest snapshot only"),
        operands=(entity("group", "customer_group_id", "group_exposure_snapshot"),
                  dim("member", "customer_id", "group_exposure_snapshot"),
                  measure("exposure", "ead", "group_exposure_snapshot",
                          economic_role="drawn_credit_exposure"),
                  policy_input("hierarchy", "ownership_percentage",
                               "group_exposure_snapshot", policy=LEGAL_HIERARCHY),
                  as_of("group_exposure_snapshot")),
        source_grain="group_exposure_snapshot", output_grain="legal_group",
        temporal=snapshot_window("hierarchy membership effective at the as-of"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="members effective in the hierarchy at the as-of, intra-group "
                     "exposures eliminated",
            excluded="departed subsidiaries; double-counted intra-group facilities",
            policy_refs=(LEGAL_HIERARCHY, GROUP_ELIMINATION, CIB_CCY)),
        formula=formula("cib", "group_exposure_aggregation", "snapshot"),
        replaces_legacy_ids=("group_exposure_aggregation",)),

    # ── the reviewed anchor stays atomic and authorable ─────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="obligor_facility_count", revision=1, family="corporate_cib",
        primary_objective=OBLIGOR,
        business_definition=(
            "Distinct facilities of the obligor with activity in the window — atomic, on "
            "its reviewed Formula-v1 count-distinct expectation."),
        decision_context="obligor complexity",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="obligor_facility_count", display_label="Obligor facility count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with no facility identity are excluded",
            empty_population_policy="no facilities returns zero"),
        operands=(entity("obligor", "obligor_id", "facility_event"),
                  dim("facility", "facility_id", "facility_event"),
                  event_ts("facility_event")),
        source_grain="facility_event", output_grain="obligor",
        temporal=event_window(),
        readiness="FORMULA_AUTHORABLE", parameters=(_WINDOW,),
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="obligor_facility_count",
                                   result_class="distinct_count"),
        replaces_legacy_ids=("obligor_facility_count",)),

    # ── guarantees carry enforceability and wrong-way policy (correction 7) ─────────────────────
    RecipeDefinitionV2(
        recipe_id="guarantor_reliance_share", revision=1, family="corporate_cib",
        primary_objective=CONCENTRATION,
        business_definition=(
            "Guarantee-backed exposure as a share of total exposure, counting only "
            "ENFORCEABLE, unexpired guarantees, weighted by guarantor quality under the "
            "wrong-way-risk policy — a guarantee expired before the cutoff protects "
            "nothing."),
        decision_context="guarantor reliance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="guarantor_reliance_share", display_label="Guarantor reliance",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="guarantees with unknown enforceability are excluded from "
                              "the numerator, exposure kept in the denominator",
            empty_population_policy="no exposure returns null",
            zero_denominator_policy="zero exposure returns null"),
        operands=(entity("obligor", "obligor_id", "guarantee_snapshot"),
                  dim("guarantor", "guarantor_id", "guarantee_snapshot"),
                  measure("guaranteed", "monetary_stock", "guarantee_snapshot"),
                  policy_input("wrong_way", "customer_risk_rating", "guarantee_snapshot",
                               policy=WRONG_WAY),
                  as_of("guarantee_snapshot")),
        source_grain="guarantee_snapshot", output_grain="obligor",
        temporal=snapshot_window("guarantees effective and unexpired at the as-of"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="enforceable unexpired guarantees under the wrong-way policy",
            excluded="expired guarantees; guarantor-obligor correlation ignored",
            policy_refs=(WRONG_WAY, CIB_CCY)),
        formula=formula("cib", "guarantor_reliance_share", "share"),
        replaces_legacy_ids=("guarantor_reliance",)),

    # ── working-capital cycle over declared periods (correction 8) ──────────────────────────────
    RecipeDefinitionV2(
        recipe_id="working_capital_cycle_days", revision=1, family="corporate_cib",
        primary_objective=WORKING_CAPITAL,
        business_definition=(
            "DSO plus DIO minus DPO over the declared AR/inventory/AP periods — the BR-6 "
            "signed-sum exemplar's shape, each term from its own declared period data."),
        decision_context="working-capital cycle",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="working_capital_cycle_days", display_label="Working-capital cycle",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="terms missing their period data return null and surface "
                              "which term",
            empty_population_policy="no period data returns null"),
        operands=(entity("obligor", "obligor_id", "working_capital_period"),
                  measure("receivables", "monetary_stock", "working_capital_period",
                          economic_role="trade_receivables"),
                  measure("payables", "monetary_stock", "working_capital_period",
                          economic_role="trade_payables"),
                  measure("revenue", "monetary_flow", "working_capital_period",
                          economic_role="recognized_customer_revenue"),
                  as_of("working_capital_period")),
        source_grain="working_capital_period", output_grain="obligor",
        temporal=snapshot_window("one declared reporting period per term"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="AR/AP/inventory/revenue from one declared period set",
            excluded="terms from mismatched periods",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib", "working_capital_cycle_days", "ratio"),
        replaces_legacy_ids=("trade_cycle_working_capital",)),

    # ── pooling: EOD utilization vs INTRADAY peak — different source grains (corrections 9-10) ──
    RecipeDefinitionV2(
        recipe_id="pool_utilisation_eod", revision=1, family="corporate_cib",
        primary_objective=WORKING_CAPITAL,
        business_definition=(
            "End-of-day pool utilization across participant accounts, per pool structure "
            "and type."),
        decision_context="cash-pool utilization (end of day)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="pool_utilisation_eod", display_label="Pool utilization (EOD)",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="participants with unknown balances are excluded and surface "
                              "as a gap",
            empty_population_policy="no pool limit returns null",
            zero_denominator_policy="a zero pool limit returns null"),
        operands=(entity("pool", "pooling_structure_id", "pool_day_snapshot"),
                  dim("participant", "account_id", "pool_day_snapshot"),
                  measure("balance", "monetary_stock", "pool_day_snapshot"),
                  as_of("pool_day_snapshot")),
        source_grain="pool_day_snapshot", output_grain="pool",
        temporal=snapshot_window("end-of-day pool snapshot"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="participant balances at end of day",
            excluded="intraday positions (the peak recipe's own grain)",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib", "pool_utilisation_eod", "ratio"),
        replaces_legacy_ids=("pooling_structure_utilisation",)),
    RecipeDefinitionV2(
        recipe_id="pool_intraday_peak", revision=1, family="corporate_cib",
        primary_objective=WORKING_CAPITAL,
        business_definition=(
            "The pool's worst INTRADAY position in the window, from sweep events with "
            "intraday timestamps — a notional pool with an intraday deficit and a positive "
            "end-of-day balance is exactly what this sees and the EOD recipe cannot: a "
            "daily snapshot structurally cannot compile an intraday peak."),
        decision_context="cash-pool intraday risk",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="pool_intraday_peak", display_label="Intraday peak deficit",
            output_type="numeric", additivity="non_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CIB_CCY,
            null_input_policy="sweeps with no intraday timestamp are excluded and surface "
                              "as a gap",
            empty_population_policy="no intraday data returns null — an EOD snapshot is "
                                    "not evidence of intraday behaviour"),
        operands=(entity("pool", "pooling_structure_id", "intraday_sweep_event"),
                  measure("position", "monetary_stock", "intraday_sweep_event"),
                  event_ts("intraday_sweep_event")),
        source_grain="intraday_sweep_event", output_grain="pool",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                window_basis="trailing", window_unit="minutes",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="sweep events with intraday timestamps",
            excluded="daily snapshots standing in for intraday positions",
            policy_refs=(CIB_CCY,)),
        formula=formula("cib", "pool_intraday_peak", "extremum"),
        replaces_legacy_ids=("pooling_structure_utilisation",)),

    # ── cross-product stress: stressed-line count as its own atom (corrections 11-12) ───────────
    RecipeDefinitionV2(
        recipe_id="stressed_line_count", revision=1, family="corporate_cib",
        primary_objective=EARLY_WARNING,
        business_definition=(
            "Count of product lines whose drawn plus contingent exposure crosses the "
            "governed stress threshold — a line with a limit but no drawn exposure counts "
            "zero, honestly."),
        decision_context="cross-product stress",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="stressed_line_count", display_label="Stressed lines",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="lines with unknown exposure are excluded and surface as a "
                              "gap",
            empty_population_policy="no product lines returns null"),
        operands=(entity("obligor", "obligor_id", "product_line_snapshot"),
                  dim("product_line", "product_id", "product_line_snapshot"),
                  measure("drawn", "drawn_principal", "product_line_snapshot",
                          economic_role="drawn_credit_exposure"),
                  measure("contingent", "contingent_exposure", "product_line_snapshot"),
                  policy_input("stress_threshold", "limit", "product_line_snapshot",
                               policy=STRESS_THRESHOLD),
                  as_of("product_line_snapshot")),
        source_grain="product_line_snapshot", output_grain="obligor",
        temporal=snapshot_window("latest product-line snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="product lines with drawn and contingent exposure against the "
                     "governed threshold",
            excluded="limits mistaken for exposure",
            policy_refs=(STRESS_THRESHOLD, CIB_CCY)),
        formula=formula("cib", "stressed_line_count", "count"),
        replaces_legacy_ids=("cross_product_stress_count",)),
)
