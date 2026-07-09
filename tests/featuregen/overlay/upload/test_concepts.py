from featuregen.overlay.upload.concepts import (
    CONCEPT_REGISTRY,
    CONCEPTS,
    UNCLASSIFIED,
    Concept,
    concept,
    humanize,
    is_known_concept,
)


def test_vocabulary_is_controlled():
    assert "monetary_amount" in CONCEPTS
    assert "account_identifier" in CONCEPTS
    assert UNCLASSIFIED not in CONCEPTS          # the fallback is not itself a concept
    assert is_known_concept("monetary_amount") is True
    assert is_known_concept("made_up_thing") is False


def test_humanize_for_search():
    assert humanize("monetary_amount") == "monetary amount"


def test_all_legacy_strings_retained():
    # The original 11 vocabulary strings must all still be known — removing one would orphan live
    # enriched columns and the current classifier's output.
    legacy = {
        "monetary_amount", "account_identifier", "customer_identifier", "as_of_date",
        "effective_date", "timestamp", "count", "rate_or_ratio", "category_code", "pii", "free_text",
    }
    for name in legacy:
        assert is_known_concept(name) is True, name


def test_registry_and_set_agree():
    assert CONCEPTS == set(CONCEPT_REGISTRY)
    assert all(isinstance(c, Concept) for c in CONCEPT_REGISTRY.values())
    # Every entry keys itself.
    assert all(name == c.name for name, c in CONCEPT_REGISTRY.items())


def test_concept_accessor():
    c = concept("monetary_stock")
    assert isinstance(c, Concept)
    assert c.name == "monetary_stock"
    assert concept("nope") is None


def test_monetary_additivity_behaviour():
    assert concept("monetary_stock").additivity == "semi_additive"
    assert concept("monetary_flow").additivity == "additive"
    assert concept("monetary_rate").additivity == "non_additive"
    assert concept("price").additivity == "non_additive"
    # notional is a position attribute -> semi_additive (never summed across snapshots) — gap-review A1.
    assert concept("notional").additivity == "semi_additive"
    # contingent_exposure is-a monetary_stock, semi-additive.
    ce = concept("contingent_exposure")
    assert ce.additivity == "semi_additive"
    assert ce.is_a == "monetary_stock"


def test_identifier_entity_links():
    assert concept("customer_id").entity_link == "customer"
    assert concept("account_id").entity_link == "account"
    assert concept("lei").entity_link == "legal_entity"
    # Identifiers do not aggregate.
    assert concept("customer_id").additivity == "n/a"
    # Legacy identifier aliases keep their entity link.
    assert concept("account_identifier").entity_link == "account"
    assert concept("customer_identifier").entity_link == "customer"


def test_temporal_pit_roles():
    assert concept("as_of_date").pit_role == "as_of"
    assert concept("effective_date").pit_role == "effective"
    assert concept("event_timestamp").pit_role == "event"
    assert concept("maturity_date").pit_role == "maturity"
    assert concept("valid_time").pit_role == "valid_time"
    assert concept("system_time").pit_role == "system_time"
    # Legacy 'timestamp' maps onto the event role.
    assert concept("timestamp").pit_role == "event"


def test_sensitivity_classes():
    assert concept("pii").sensitivity == "pii"
    assert concept("protected_attribute").sensitivity == "protected_attribute"
    assert concept("special_category").sensitivity == "special_category"
    # geographic is a fair-lending proxy.
    assert concept("geographic").sensitivity == "proxy"


def test_leakage_anchors():
    assert concept("outcome_label").leakage_anchor is True
    assert concept("default_flag").leakage_anchor is True
    assert concept("fraud_flag").leakage_anchor is True
    assert concept("delinquency_flag").leakage_anchor is True
    # A non-target flag is not an anchor.
    assert concept("boolean_flag").leakage_anchor is False


def test_currency_code_is_the_unit():
    c = concept("currency_code")
    assert c is not None
    assert c.group == "currency"
    # The non-mixable rule is documented on the concept.
    assert "mix" in c.description.lower()


def test_regulatory_capital_and_esg_additivity():
    # gap-review A1 correctness fix: these are STOCKS (balances/snapshots), so semi_additive — summing a
    # stock across reporting dates is a wrong number (was wrongly tagged additive). is_a monetary_stock.
    for stock in ("rwa", "ead", "ecl", "provision_amount"):
        assert concept(stock).additivity == "semi_additive", stock
        assert concept(stock).is_a == "monetary_stock", stock
    assert concept("pd_ttc").additivity == "non_additive"
    assert concept("pd_pit").additivity == "non_additive"
    assert concept("risk_weight").additivity == "non_additive"
    assert concept("capital_ratio").additivity == "non_additive"
    assert concept("carbon_intensity").additivity == "non_additive"


def test_score_probability_flags_leakage_risk_in_description():
    for name in ("score_probability", "pd"):
        assert "leakage" in concept(name).description.lower(), name


def test_registry_is_meaningfully_expanded():
    # Structured registry, not the old flat 11.
    assert len(CONCEPT_REGISTRY) >= 70
    # Groups used are all from the controlled set.
    allowed_groups = {
        "monetary", "identifier", "temporal", "quantity_risk", "categorical", "geographic", "flag",
        "sensitive", "text", "label", "behavioural", "network", "bitemporal", "currency",
        "eligibility", "regulatory_capital", "accounting", "esg", "crypto",
    }
    assert {c.group for c in CONCEPT_REGISTRY.values()} <= allowed_groups


def test_classification_vocabulary_offers_rich_concepts_excludes_legacy():
    from featuregen.overlay.upload.concepts import classification_vocabulary
    vocab = classification_vocabulary()
    names = {v["name"] for v in vocab}
    assert "monetary_stock" in names and "outcome_label" in names        # rich §3 concepts are targets
    assert "monetary_amount" not in names and "rate_or_ratio" not in names  # legacy aliases are not
    assert "unclassified" not in names
    ms = next(v for v in vocab if v["name"] == "monetary_stock")
    assert ms["group"] == "monetary" and ms["hint"]                       # name + group + short hint


def test_gap_review_phase1_fixes():
    # A2 — personal/proxy data no longer 'public', so the eligibility/read-scope gate can fire.
    for pii_c in ("geolocation", "device_fingerprint", "free_text", "unstructured_doc", "pep_flag",
                  "sanctions_hit_flag", "beneficiary_name"):
        assert concept(pii_c).sensitivity == "pii", pii_c
    assert concept("country_code").sensitivity == "proxy"                 # national-origin proxy
    # A3 — near-label: funnel-tail signals that border the target (must be flagged, not hard-blocked).
    for nl in ("restructured_flag", "impairment_stage", "sanctions_hit_flag"):
        assert concept(nl).near_label is True, nl
    # B — pilot-unblocking concepts now exist (churn Stage-4 unbundling + primacy + cash-flow direction).
    for c in ("direct_debit", "standing_order", "beneficiary_name", "beneficiary_bank",
              "debit_credit_indicator"):
        assert concept(c) is not None, c


def test_gap_review_phase2_additive_expansion():
    # ADDITIVE Phase-2 growth: sample one concept from each new cluster (each must ground).
    cluster_samples = [
        # wholesale / markets
        "limit", "limit_type", "covenant", "collateral_type", "lien_seniority", "netting_set_id",
        "margin", "syndication_share", "lcr", "nsfr", "hqla", "pv01", "dv01", "repricing_gap",
        "ftp_rate", "invoice_id", "implied_volatility", "position_direction", "expected_exposure",
        "potential_future_exposure", "expected_shortfall",
        # risk & credit
        "macro_variable", "scenario_id", "scenario_weight", "recovery_amount", "write_off_amount",
        "cost_to_collect", "bureau_score", "bureau_inquiry", "trade_line", "sicr_flag",
        "delinquency_bucket", "exposure_class", "customer_risk_rating", "expected_loss",
        "lifetime_pd", "effective_maturity", "npe_flag", "watchlist_hit_flag", "adverse_media_flag",
        "collateral_value", "ownership_percentage", "model_tier",
        # insurance / custody / asset-mgmt
        "premium", "claim_reserve", "sum_assured", "surrender_value", "reinsurance_recoverable",
        "mortality_morbidity", "nav", "settlement_status", "settlement_cycle", "corporate_action",
        "record_date", "ex_date", "pay_date", "securities_loan", "custody_holding", "fund",
        "share_class", "fund_flow", "mandate", "benchmark", "tracking_error", "expense_ratio",
        # islamic / esg / payments
        "profit_rate", "profit_share_ratio", "purification_amount", "prohibited_activity_exposure",
        "sukuk", "takaful_contribution", "scope_1_emissions", "scope_2_emissions",
        "scope_3_emissions", "financed_emissions", "taxonomy_alignment", "emissions_data_quality",
        "physical_hazard_score", "transition_alignment", "sll_kpi", "payment_rail", "scheme",
        "interchange", "merchant_discount_rate", "corridor", "settlement_finality", "nostro_vostro",
        "iso20022_purpose_code",
        # cross-cutting
        "reference_data", "model_output", "data_quality_flag", "source_system", "segment",
        "peer_group", "scheduled_amount", "unit_of_measure", "vulnerability_flag", "household_id",
        "portfolio_id", "book_id", "desk_id", "bureau_provenance", "collateral_id", "policy_id",
        "claim_id", "case_id", "alert_id", "campaign_id", "relationship_manager_id", "gl_account",
        "obligor_id", "guarantor_id",
        # specialist near-labels
        "lapsed", "surrendered", "settlement_fail", "redeemed",
        # still-missing areas
        "regulatory_report_line", "anacredit_attribute", "finrep_corep_line",
        "mifir_transaction_report", "emir_report", "fatca_crs_classification", "consent_token",
        "tpp_id", "aisp_pisp_flag", "api_call_event", "digital_asset", "wallet_address",
        "stablecoin", "on_chain_txn", "cbdc", "tranche", "spv_id", "waterfall_position",
        "credit_enhancement", "contribution", "annuity_factor", "vesting", "decumulation",
        "loss_event", "loss_amount", "risk_control_id", "near_miss_flag", "withholding_amount",
        "tax_lot", "taxable_flag", "alternative_data", "thin_file_flag",
        "cashflow_underwriting_signal", "tlac_mrel", "wholesale_funding", "resolution_group",
        "complaint_event", "redress_amount", "root_cause_code", "swift_message_type",
        "nested_correspondent_flag", "biodiversity_impact", "deforestation_flag",
    ]
    for name in cluster_samples:
        assert concept(name) is not None, name

    # limit is a CEILING, not a balance: not naively additive, and NOT is_a monetary_stock (§B/§E).
    lim = concept("limit")
    assert lim.additivity != "additive"
    assert lim.is_a is None

    # premium: the written-vs-earned additivity trap must be annotated on the concept.
    pdesc = concept("premium").description.lower()
    assert "written" in pdesc and "earned" in pdesc

    # ESG emissions: the cross-scope / cross-entity double-count trap is annotated.
    assert "double-count" in concept("scope_3_emissions").description.lower()

    # vulnerability_flag is sensitive (FCA Consumer Duty) so the read-scope/eligibility gate can fire.
    assert concept("vulnerability_flag").sensitivity != "public"

    # New entity_links resolve (identifiers do not aggregate).
    assert concept("netting_set_id").entity_link == "netting_set"
    assert concept("household_id").entity_link == "household"
    assert concept("obligor_id").entity_link == "obligor"
    assert concept("netting_set_id").additivity == "n/a"

    # Islamic profit_rate is deliberately NOT is_a monetary_rate (interest) — a compliance distinction.
    assert concept("profit_rate").is_a is None
    assert "interest" in concept("profit_rate").description.lower()

    # Score lineage: an external bureau score is_a the abstract score concept.
    assert concept("bureau_score").is_a == "score_probability"

    # New group 'crypto' is used and stays within the controlled set (asserted elsewhere).
    assert concept("digital_asset").group == "crypto"

    # Specialist near-label outcomes are leakage anchors that generalise to outcome_label.
    for nl in ("lapsed", "surrendered", "settlement_fail", "redeemed"):
        assert concept(nl).leakage_anchor is True, nl
        assert concept(nl).is_a == "outcome_label", nl


def test_all_is_a_edges_resolve():
    # Every is_a must point at a real concept (also enforced at import by _validate_registry).
    for c in CONCEPT_REGISTRY.values():
        if c.is_a is not None:
            assert c.is_a in CONCEPT_REGISTRY, (c.name, c.is_a)
