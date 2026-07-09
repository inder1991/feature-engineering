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
        "eligibility", "regulatory_capital", "accounting", "esg",
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
