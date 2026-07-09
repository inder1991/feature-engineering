"""Phase-3 Pass-6 (the FINAL breadth pass) — the §B5 cross-sell / CLV GROWTH journey and the §B15
corporate / SME trade & supply-chain-finance set, authored to Part-F depth. These two families complete
the 15-family library.

Two domain-shaped mini-catalogs are built via ``build_graph``:
  • a CROSS-SELL catalog at customer grain (monetary_flow / product_type / product_id / segment /
    peer_group / channel / campaign_id / household_id / relationship_manager_id + event_ts / as_of /
    effective_date), and
  • a CORPORATE / TRADE catalog at facility/obligor grain (limit / limit_type / contingent_exposure /
    covenant / syndication_share / collateral_type / ownership_percentage / invoice_id / obligor_id /
    guarantor_id / pooling_structure_id + a drawn monetary_stock / an invoice monetary_flow),
each carrying two DANGEROUS columns — a leakage anchor (``outcome_label`` = the "purchased/converted"
label for cross-sell; ``default_flag`` for corporate) and a ``protected_attribute`` — to prove the engine
NEVER binds them. Grounding is deterministic (no LLM), so these are exact assertions.

Routing is the locked, first-class assertion (⚠ the HARDEST case for CLV): cross-sell/CLV is the INVERSE
of churn and SHARES its generic concepts (monetary_flow / event_ts / customer_id), so a recipe needing
ONLY those would cross-surface. Each recipe additionally requires a NON-STRUCTURAL distinctive concept, so
each new family grounds NOTHING on the churn catalog, and ``ALL_TEMPLATES`` on the churn catalog still
yields exactly the churn lens.
"""
from tests.featuregen.overlay.upload.test_templates import _CATALOG as _CHURN_CATALOG
from tests.featuregen.overlay.upload.test_templates import SOURCE as CHURN_SOURCE
from tests.featuregen.overlay.upload.test_templates import _churn_catalog

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    CORPORATE_TRADE_TEMPLATES,
    CROSS_SELL_TEMPLATES,
    RETAIL_CHURN_TEMPLATES,
    GroundedFeature,
    ground_all,
    ground_template,
)


def _by_id(templates):
    return {t.id: t for t in templates}


XS = _by_id(CROSS_SELL_TEMPLATES)
CORP = _by_id(CORPORATE_TRADE_TEMPLATES)


def _build(db, source, catalog):
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog if c}
    build_graph(db, source, rows, concepts=concepts)


# ── cross-sell catalog — the growth journey, at customer grain ──────────────────────────────────────
XS_SOURCE = "cross_sell"
_XS_CATALOG = [
    (CanonicalRow(XS_SOURCE, "customers", "customer_id", "integer", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(XS_SOURCE, "customers", "signup_dt", "date"), "effective_date"),
    (CanonicalRow(XS_SOURCE, "customers", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    # cross-sell-distinctive, NON-STRUCTURAL anchors (every recipe requires one)
    (CanonicalRow(XS_SOURCE, "customers", "segment", "text"), "segment"),
    (CanonicalRow(XS_SOURCE, "customers", "peer_group", "text"), "peer_group"),
    (CanonicalRow(XS_SOURCE, "holdings", "product_type", "text"), "product_type"),
    # entity concepts — the aggregation grain / links (structural, never the sole anchor)
    (CanonicalRow(XS_SOURCE, "holdings", "product_id", "integer", entity="Product"), "product_id"),
    (CanonicalRow(XS_SOURCE, "customers", "household_id", "integer", entity="Household"), "household_id"),
    (CanonicalRow(XS_SOURCE, "customers", "rm_id", "integer", entity="RelationshipManager"),
     "relationship_manager_id"),
    (CanonicalRow(XS_SOURCE, "campaigns", "campaign_id", "integer", entity="Campaign"), "campaign_id"),
    # generic (SHARED with churn) — never a sole anchor here
    (CanonicalRow(XS_SOURCE, "txns", "amount", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(XS_SOURCE, "txns", "txn_ts", "timestamp"), "event_timestamp"),
    (CanonicalRow(XS_SOURCE, "txns", "channel", "text"), "channel"),
    # DANGEROUS — never a feature input (deliberately NOT is_grain / is_as_of)
    (CanonicalRow(XS_SOURCE, "customers", "purchased", "boolean"), "outcome_label"),     # leakage (converted)
    (CanonicalRow(XS_SOURCE, "customers", "age_band", "text"), "protected_attribute"),   # protected
]
_XS_DANGEROUS = {"public.customers.purchased", "public.customers.age_band"}
_ALL_XS_IDS = {t.id for t in CROSS_SELL_TEMPLATES}


# ── corporate / trade catalog — trade & supply-chain finance, at facility/obligor grain ─────────────
CORP_SOURCE = "corporate_trade"
_CORP_CATALOG = [
    (CanonicalRow(CORP_SOURCE, "facilities", "facility_id", "integer", is_grain=True, entity="Facility"),
     "facility_id"),
    (CanonicalRow(CORP_SOURCE, "facilities", "obligor_id", "integer", entity="Obligor"), "obligor_id"),
    (CanonicalRow(CORP_SOURCE, "facilities", "guarantor_id", "integer", entity="Guarantor"),
     "guarantor_id"),
    (CanonicalRow(CORP_SOURCE, "facilities", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    # corporate-distinctive, NON-STRUCTURAL anchors (every recipe requires one)
    (CanonicalRow(CORP_SOURCE, "facilities", "credit_limit", "numeric", additivity="semi_additive",
                  currency="USD"), "limit"),
    (CanonicalRow(CORP_SOURCE, "facilities", "limit_type", "text"), "limit_type"),
    (CanonicalRow(CORP_SOURCE, "facilities", "contingent", "numeric", additivity="semi_additive",
                  currency="USD"), "contingent_exposure"),
    (CanonicalRow(CORP_SOURCE, "facilities", "drawn", "numeric", additivity="semi_additive",
                  currency="USD"), "monetary_stock"),
    (CanonicalRow(CORP_SOURCE, "facilities", "covenant_headroom", "numeric"), "covenant"),  # near-label
    (CanonicalRow(CORP_SOURCE, "facilities", "syndication_share", "numeric"), "syndication_share"),
    (CanonicalRow(CORP_SOURCE, "facilities", "collateral_type", "text"), "collateral_type"),
    (CanonicalRow(CORP_SOURCE, "facilities", "ownership_pct", "numeric"), "ownership_percentage"),
    # entity concepts — the receivables / pool grain (structural, never the sole anchor)
    (CanonicalRow(CORP_SOURCE, "invoices", "invoice_id", "integer", entity="Invoice"), "invoice_id"),
    (CanonicalRow(CORP_SOURCE, "invoices", "amount", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(CORP_SOURCE, "invoices", "invoice_ts", "timestamp"), "event_timestamp"),
    (CanonicalRow(CORP_SOURCE, "pools", "pooling_structure_id", "integer", entity="PoolingStructure"),
     "pooling_structure_id"),
    # DANGEROUS — never a feature input (deliberately NOT is_grain / is_as_of)
    (CanonicalRow(CORP_SOURCE, "facilities", "defaulted", "boolean"), "default_flag"),         # leakage
    (CanonicalRow(CORP_SOURCE, "facilities", "borrower_age_band", "text"), "protected_attribute"),  # protected
]
_CORP_DANGEROUS = {"public.facilities.defaulted", "public.facilities.borrower_age_band"}
_ALL_CORP_IDS = {t.id for t in CORPORATE_TRADE_TEMPLATES}


# ══ authored the two families (the final breadth pass — completes the 15-family library) ═════════════
def test_growth_trade_families_authored():
    assert len(CROSS_SELL_TEMPLATES) == 10
    assert len(CORPORATE_TRADE_TEMPLATES) == 11
    assert set(XS) == {
        "channel_adoption_depth", "product_gap_whitespace", "next_best_product_propensity",
        "relationship_deepening_breadth", "campaign_response_recency", "clv_revenue_trajectory",
        "share_of_wallet_growth", "segment_relative_penetration", "household_relationship_value",
        "tenure_upsell_readiness"}
    assert set(CORP) == {
        "facility_utilisation_headroom", "lc_guarantee_rollover", "invoice_finance_dynamics",
        "supply_chain_finance_dynamics", "covenant_headroom_breach", "syndication_concentration",
        "group_exposure_aggregation", "guarantor_reliance", "trade_cycle_working_capital",
        "pooling_structure_utilisation", "cross_product_stress_count"}
    # every need references a real concept (also enforced at import by _validate_registry)
    for t in CROSS_SELL_TEMPLATES + CORPORATE_TRADE_TEMPLATES:
        for need in t.needs:
            assert need.concept in CONCEPT_REGISTRY


# ══ ROUTING (the locked invariant): every recipe anchors on a NON-STRUCTURAL distinctive concept ═════
def test_every_growth_trade_recipe_anchors_on_a_non_structural_distinctive_concept():
    # A recipe must REQUIRE at least one concept that is (a) outside the churn catalog's vocabulary AND
    # (b) non-structural (not an entity/as_of concept — those get structural is_grain/is_as_of credit in
    # _match and would bind a churn grain/as-of column, cross-surfacing). This is precisely the condition
    # the runtime CLV routing test below enforces — the hardest case, since CLV shares churn's generics.
    churn_concepts = {c for _r, c in _CHURN_CATALOG if c}
    for t in CROSS_SELL_TEMPLATES + CORPORATE_TRADE_TEMPLATES:
        distinctive = False
        for n in t.needs:
            if n.optional:
                continue
            c = CONCEPT_REGISTRY[n.concept]
            structural = c.entity_link is not None or c.pit_role == "as_of"
            if not structural and n.concept not in churn_concepts:
                distinctive = True
        assert distinctive, f"{t.id} has no non-structural distinctive required need (would cross-surface)"


# ══ the families ground on their domain-shaped catalogs (a healthy subset — here the whole family) ═══
def test_cross_sell_family_grounds_on_a_cross_sell_catalog(db):
    _build(db, XS_SOURCE, _XS_CATALOG)
    grounded = ground_all(db, CROSS_SELL_TEMPLATES, catalog_source=XS_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_XS_IDS   # the whole growth journey realizes here


def test_corporate_trade_family_grounds_on_a_corporate_catalog(db):
    _build(db, CORP_SOURCE, _CORP_CATALOG)
    grounded = ground_all(db, CORPORATE_TRADE_TEMPLATES, catalog_source=CORP_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_CORP_IDS   # the whole trade/SCF set realizes here


def test_clv_revenue_grounds_to_product_flow_event_customer(db):
    _build(db, XS_SOURCE, _XS_CATALOG)
    gf = ground_template(db, XS["clv_revenue_trajectory"], catalog_source=XS_SOURCE)
    assert isinstance(gf, GroundedFeature)
    assert gf.name == "clv_revenue_trajectory_365d"       # id + default window (first allowed = 365)
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.holdings.product_type" in refs         # the distinctive anchor (routes it off churn)
    assert "public.txns.amount" in refs                   # the monetary_flow revenue
    assert "public.customers.customer_id" in refs         # the entity/grain
    assert gf.additivity == "additive" and gf.near_label is False
    assert any("PROJECTION" in n for n in gf.notes)       # CLV is a declared projection (no data plane)


def test_covenant_headroom_grounds_to_covenant_asof_obligor(db):
    _build(db, CORP_SOURCE, _CORP_CATALOG)
    gf = ground_template(db, CORP["covenant_headroom_breach"], catalog_source=CORP_SOURCE)
    assert gf is not None and gf.name == "covenant_headroom_breach_90d"   # default window (first = 90)
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.facilities.covenant_headroom" in refs  # the near-label covenant anchor
    assert gf.additivity == "non_additive" and gf.near_label is True


def test_group_exposure_grounds_on_ownership_percentage(db):
    _build(db, CORP_SOURCE, _CORP_CATALOG)
    gf = ground_template(db, CORP["group_exposure_aggregation"], catalog_source=CORP_SOURCE)
    assert gf is not None
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.facilities.ownership_pct" in refs      # the group-consolidation-weight anchor
    assert gf.additivity == "semi_additive"               # a group exposure stock


# ══ safety by construction — the engine NEVER binds the leakage anchor or a protected column ═════════
def test_cross_sell_never_binds_the_conversion_label_or_protected_columns(db):
    # THE headline: a cross-sell propensity must NOT read the purchased/converted outcome label.
    _build(db, XS_SOURCE, _XS_CATALOG)
    grounded = ground_all(db, CROSS_SELL_TEMPLATES, catalog_source=XS_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _XS_DANGEROUS)                 # conversion label + protected attr never bound


def test_corporate_trade_never_binds_the_target_or_protected_columns(db):
    _build(db, CORP_SOURCE, _CORP_CATALOG)
    grounded = ground_all(db, CORPORATE_TRADE_TEMPLATES, catalog_source=CORP_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _CORP_DANGEROUS)               # default_flag + protected attr never bound


# ══ near-label recipes carry near_label True (the 3-part leakage control must flag them) ═════════════
def test_cross_sell_has_no_near_label_recipes():
    # the growth journey's "conversion" is a HARD leakage anchor, not a bordering near-label.
    assert {t.id for t in CROSS_SELL_TEMPLATES if t.near_label} == set()


def test_corporate_covenant_recipe_is_the_only_near_label_and_is_flagged(db):
    _build(db, CORP_SOURCE, _CORP_CATALOG)
    assert {t.id for t in CORPORATE_TRADE_TEMPLATES if t.near_label} == {"covenant_headroom_breach"}
    gf = ground_template(db, CORP["covenant_headroom_breach"], catalog_source=CORP_SOURCE)
    assert gf is not None and gf.near_label is True
    assert "NEAR-LABEL" in gf.eligibility                 # the ⚠ pre-breach note travels onto the candidate


# ══ a required distinctive need absent -> the recipe SKIPS (degrade path) ════════════════════════════
def test_recipe_skips_when_its_distinctive_concept_is_absent(db):
    # a corporate catalog with ONLY 'limit' (+ drawn/as_of/facility grain) grounds the limit-anchored
    # recipes but nothing that needs covenant / syndication_share / contingent / ownership / collateral.
    # (pooling_structure_utilisation is limit-anchored too — its pooling_structure_id ENTITY need binds
    # the facility grain structurally, exactly as the routing design intends.)
    catalog = [
        (CanonicalRow("mini_corp", "f", "facility_id", "integer", is_grain=True, entity="Facility"),
         "facility_id"),
        (CanonicalRow("mini_corp", "f", "obligor_id", "integer", entity="Obligor"), "obligor_id"),
        (CanonicalRow("mini_corp", "f", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("mini_corp", "f", "credit_limit", "numeric", additivity="semi_additive",
                      currency="USD"), "limit"),
        (CanonicalRow("mini_corp", "f", "drawn", "numeric", additivity="semi_additive", currency="USD"),
         "monetary_stock"),
    ]
    _build(db, "mini_corp", catalog)
    grounded = {gf.template_id for gf in ground_all(db, CORPORATE_TRADE_TEMPLATES,
                                                    catalog_source="mini_corp")}
    assert grounded == {"facility_utilisation_headroom", "pooling_structure_utilisation",
                        "cross_product_stress_count"}    # exactly the three limit-anchored recipes
    assert "covenant_headroom_breach" not in grounded    # needs the covenant anchor (absent)
    assert "syndication_concentration" not in grounded   # needs the syndication_share anchor (absent)


# ══ ROUTING: neither family cross-surfaces on a churn catalog (the KEY guard — esp. CLV) ═════════════
def test_growth_trade_families_do_not_ground_on_a_churn_catalog(db):
    _churn_catalog(db)                                    # the pilot churn catalog — no growth/trade concepts
    for family in (CROSS_SELL_TEMPLATES, CORPORATE_TRADE_TEMPLATES):
        grounded = ground_all(db, family, catalog_source=CHURN_SOURCE, roles=("pii_reader",))
        assert grounded == []                             # grounding routed the whole family away


def test_all_templates_on_a_churn_catalog_still_yields_only_the_churn_lens(db):
    # The locked churn=churn-lens invariant MUST stay green even after the CLV family (which SHARES
    # churn's generic monetary_flow / event_ts / customer_id) joins ALL_TEMPLATES.
    _churn_catalog(db)
    churn_only = {gf.template_id for gf in
                  ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    combined = {gf.template_id for gf in
                ground_all(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    assert combined == churn_only                         # ALL_TEMPLATES adds no growth/trade ids on churn
    assert not (combined & (_ALL_XS_IDS | _ALL_CORP_IDS))
