"""Phase-3 Pass-4 breadth families — the §B8 markets/trading risk families + counterparty-risk funnel,
the §B10 custody settlement-fail funnel, and the §B12 asset-management redemption funnel + mandate
compliance, authored to Part-F depth.

Three domain-shaped mini-catalogs are built via ``build_graph``:
  • a TRADING catalog (instrument grain + book/netting-set/counterparty/desk entities · var /
    expected_shortfall · pv01 / dv01 / implied_volatility · notional / position_direction ·
    expected_exposure / potential_future_exposure · margin · limit · benchmark_rate / price ·
    watchlist_hit_flag / adverse_media_flag),
  • a CUSTODY catalog (account grain + instrument · settlement_status / settlement_cycle ·
    corporate_action · securities_loan · nav · custody_holding · record_date / pay_date), and
  • an ASSET-MGMT catalog (fund grain + share_class · fund_flow · benchmark · tracking_error ·
    expense_ratio · nav · a monetary_stock AUM · peer_group),
each carrying two DANGEROUS columns — a leakage anchor (``default_flag`` / ``settlement_fail`` /
``redeemed``) and a ``protected_attribute`` — to prove the engine NEVER binds them. Grounding is
deterministic (no LLM), so these are exact assertions. The headline SAFETY test: a settlement-fail
prediction must NOT read ``settlement_fail``, and a redemption recipe must NOT read ``redeemed`` — each is
built from PRE-outcome signals (settlement_status / fund_flow) instead.

Routing is the locked, first-class assertion: grounding is the ROUTER — a family surfaces only where its
distinctive, NON-STRUCTURAL concepts exist, so each new family grounds NOTHING on the churn catalog, and
``ALL_TEMPLATES`` on the churn catalog still yields exactly the churn lens.
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
    ASSET_MGMT_TEMPLATES,
    CUSTODY_TEMPLATES,
    MARKETS_TEMPLATES,
    RETAIL_CHURN_TEMPLATES,
    GroundedFeature,
    ground_all,
    ground_template,
)


def _by_id(templates):
    return {t.id: t for t in templates}


MKT = _by_id(MARKETS_TEMPLATES)
CUS = _by_id(CUSTODY_TEMPLATES)
AM = _by_id(ASSET_MGMT_TEMPLATES)


def _build(db, source, catalog):
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog if c}
    build_graph(db, source, rows, concepts=concepts)


# ── trading catalog — markets risk families + counterparty-risk funnel, at instrument grain ──────────
MKT_SOURCE = "trading"
_MKT_CATALOG = [
    # instrument grain + the book / netting-set / counterparty / desk entities
    (CanonicalRow(MKT_SOURCE, "positions", "instrument_id", "integer", is_grain=True, entity="Instrument"),
     "instrument_id"),
    (CanonicalRow(MKT_SOURCE, "positions", "book_id", "integer", entity="Book"), "book_id"),
    (CanonicalRow(MKT_SOURCE, "positions", "desk_id", "integer", entity="Desk"), "desk_id"),
    (CanonicalRow(MKT_SOURCE, "positions", "netting_set_id", "integer", entity="NettingSet"),
     "netting_set_id"),
    (CanonicalRow(MKT_SOURCE, "positions", "counterparty_id", "integer", entity="Counterparty"),
     "counterparty_id"),
    (CanonicalRow(MKT_SOURCE, "positions", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(MKT_SOURCE, "positions", "call_ts", "timestamp"), "event_timestamp"),
    # position attributes
    (CanonicalRow(MKT_SOURCE, "positions", "notional", "numeric", additivity="semi_additive",
                  currency="USD"), "notional"),
    (CanonicalRow(MKT_SOURCE, "positions", "direction", "text"), "position_direction"),
    (CanonicalRow(MKT_SOURCE, "positions", "price", "numeric"), "price"),
    # market-risk measures + greeks (markets-distinctive, non-structural anchors)
    (CanonicalRow(MKT_SOURCE, "risk", "var_1d", "numeric"), "var"),
    (CanonicalRow(MKT_SOURCE, "risk", "es_1d", "numeric"), "expected_shortfall"),
    (CanonicalRow(MKT_SOURCE, "risk", "pv01", "numeric"), "pv01"),
    (CanonicalRow(MKT_SOURCE, "risk", "dv01", "numeric"), "dv01"),
    (CanonicalRow(MKT_SOURCE, "risk", "implied_vol", "numeric"), "implied_volatility"),
    # counterparty exposure + margin
    (CanonicalRow(MKT_SOURCE, "xva", "epe", "numeric", additivity="semi_additive", currency="USD"),
     "expected_exposure"),
    (CanonicalRow(MKT_SOURCE, "xva", "pfe", "numeric"), "potential_future_exposure"),
    (CanonicalRow(MKT_SOURCE, "xva", "posted_margin", "numeric", additivity="semi_additive",
                  currency="USD"), "margin"),
    # trading limit + benchmark
    (CanonicalRow(MKT_SOURCE, "limits", "trading_limit", "numeric", currency="USD"), "limit"),
    (CanonicalRow(MKT_SOURCE, "risk", "sofr", "numeric"), "benchmark_rate"),
    # counterparty screening (watchlist public; adverse-media pii)
    (CanonicalRow(MKT_SOURCE, "counterparties", "watchlisted", "boolean"), "watchlist_hit_flag"),
    (CanonicalRow(MKT_SOURCE, "counterparties", "adverse_media", "boolean", sensitivity="pii"),
     "adverse_media_flag"),
    # DANGEROUS — never a feature input (deliberately NOT is_grain / is_as_of)
    (CanonicalRow(MKT_SOURCE, "positions", "cpty_default", "boolean"), "default_flag"),       # leakage
    (CanonicalRow(MKT_SOURCE, "positions", "trader_age_band", "text"), "protected_attribute"),  # protected
]
_MKT_DANGEROUS = {"public.positions.cpty_default", "public.positions.trader_age_band"}
_ALL_MKT_IDS = {t.id for t in MARKETS_TEMPLATES}


# ── custody catalog — the settlement-fail funnel, at custody-account grain ───────────────────────────
CUS_SOURCE = "custody"
_CUS_CATALOG = [
    (CanonicalRow(CUS_SOURCE, "instructions", "account_id", "integer", is_grain=True, entity="Account"),
     "account_id"),
    (CanonicalRow(CUS_SOURCE, "instructions", "instrument_id", "integer", entity="Instrument"),
     "instrument_id"),
    (CanonicalRow(CUS_SOURCE, "instructions", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(CUS_SOURCE, "instructions", "event_ts", "timestamp"), "event_timestamp"),
    # settlement lifecycle (custody-distinctive, non-structural — the PRE-fail signals)
    (CanonicalRow(CUS_SOURCE, "instructions", "settlement_status", "text"), "settlement_status"),
    (CanonicalRow(CUS_SOURCE, "instructions", "settlement_cycle", "text"), "settlement_cycle"),
    # asset-servicing
    (CanonicalRow(CUS_SOURCE, "corp_actions", "ca_event", "text"), "corporate_action"),
    (CanonicalRow(CUS_SOURCE, "corp_actions", "record_dt", "date"), "record_date"),
    (CanonicalRow(CUS_SOURCE, "corp_actions", "pay_dt", "date"), "pay_date"),
    (CanonicalRow(CUS_SOURCE, "lending", "on_loan", "numeric", additivity="semi_additive",
                  currency="USD"), "securities_loan"),
    (CanonicalRow(CUS_SOURCE, "fund_admin", "nav", "numeric"), "nav"),
    (CanonicalRow(CUS_SOURCE, "holdings", "auc_holding", "numeric", additivity="semi_additive",
                  currency="USD"), "custody_holding"),
    # DANGEROUS — never a feature input (settlement_fail = the specialist leakage anchor)
    (CanonicalRow(CUS_SOURCE, "instructions", "failed", "boolean"), "settlement_fail"),        # leakage
    (CanonicalRow(CUS_SOURCE, "instructions", "client_age_band", "text"), "protected_attribute"),  # prot
]
_CUS_DANGEROUS = {"public.instructions.failed", "public.instructions.client_age_band"}
_ALL_CUS_IDS = {t.id for t in CUSTODY_TEMPLATES}


# ── asset-management catalog — the redemption funnel + mandate compliance, at fund grain ─────────────
AM_SOURCE = "asset_mgmt"
_AM_CATALOG = [
    (CanonicalRow(AM_SOURCE, "funds", "fund_id", "integer", is_grain=True, entity="Fund"), "fund"),
    (CanonicalRow(AM_SOURCE, "share_classes", "share_class_id", "integer", entity="ShareClass"),
     "share_class"),
    (CanonicalRow(AM_SOURCE, "funds", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(AM_SOURCE, "flows", "flow_ts", "timestamp"), "event_timestamp"),
    # asset-management-distinctive, non-structural anchors (built from these, NEVER 'redeemed')
    (CanonicalRow(AM_SOURCE, "flows", "net_flow", "numeric", additivity="additive", currency="USD"),
     "fund_flow"),
    (CanonicalRow(AM_SOURCE, "funds", "benchmark_index", "text"), "benchmark"),
    (CanonicalRow(AM_SOURCE, "funds", "tracking_error", "numeric"), "tracking_error"),
    (CanonicalRow(AM_SOURCE, "funds", "ter", "numeric"), "expense_ratio"),
    (CanonicalRow(AM_SOURCE, "funds", "nav", "numeric"), "nav"),
    (CanonicalRow(AM_SOURCE, "funds", "aum", "numeric", additivity="semi_additive", currency="USD"),
     "monetary_stock"),
    (CanonicalRow(AM_SOURCE, "funds", "ima_mandate", "text"), "mandate"),
    (CanonicalRow(AM_SOURCE, "funds", "peer_cohort", "text"), "peer_group"),
    # DANGEROUS — never a feature input (redeemed = the specialist leakage anchor)
    (CanonicalRow(AM_SOURCE, "funds", "redeemed", "boolean"), "redeemed"),                     # leakage
    (CanonicalRow(AM_SOURCE, "funds", "manager_gender", "text"), "protected_attribute"),       # protected
]
_AM_DANGEROUS = {"public.funds.redeemed", "public.funds.manager_gender"}
_ALL_AM_IDS = {t.id for t in ASSET_MGMT_TEMPLATES}


# ══ authored the three families (markets 9 · custody 8 · asset-mgmt 8) ════════════════════════════════
def test_breadth_families_authored():
    assert len(MARKETS_TEMPLATES) == 9
    assert len(CUSTODY_TEMPLATES) == 8
    assert len(ASSET_MGMT_TEMPLATES) == 8
    assert set(MKT) == {
        "position_var_risk", "greek_sensitivity_exposure", "notional_netting_exposure",
        "counterparty_exposure_trend", "margin_call_intensity", "trading_limit_utilisation",
        "book_desk_concentration", "benchmark_basis_dislocation", "counterparty_deterioration_ewi"}
    assert set(CUS) == {
        "matching_break_rate", "pre_settlement_aging", "settlement_fail_rate", "fail_ageing_buckets",
        "corporate_action_complexity", "sec_lending_utilisation", "nav_strike_timeliness",
        "custody_holding_dynamics"}
    assert set(AM) == {
        "net_fund_flow_trend", "performance_vs_benchmark", "share_class_flow_mix",
        "redemption_liquidity_coverage", "aum_stability", "tracking_error_breach_proximity",
        "mandate_breach_proximity", "expense_ratio_competitiveness"}
    # every need references a real concept (also enforced at import by _validate_registry)
    for t in MARKETS_TEMPLATES + CUSTODY_TEMPLATES + ASSET_MGMT_TEMPLATES:
        for need in t.needs:
            assert need.concept in CONCEPT_REGISTRY


# ══ ROUTING (the locked invariant): every recipe anchors on a NON-STRUCTURAL distinctive concept ═════
def test_every_breadth_recipe_anchors_on_a_non_structural_distinctive_concept():
    # A recipe must REQUIRE at least one concept that is (a) outside the churn catalog's vocabulary AND
    # (b) non-structural (not an entity/as_of concept — those get structural is_grain/is_as_of credit in
    # _match and would bind a churn grain/as-of column, cross-surfacing the family). This is the precise
    # condition the runtime routing test below enforces.
    churn_concepts = {c for _r, c in _CHURN_CATALOG if c}
    for t in MARKETS_TEMPLATES + CUSTODY_TEMPLATES + ASSET_MGMT_TEMPLATES:
        distinctive = False
        for n in t.needs:
            if n.optional:
                continue
            c = CONCEPT_REGISTRY[n.concept]
            structural = c.entity_link is not None or c.pit_role == "as_of"
            if not structural and n.concept not in churn_concepts:
                distinctive = True
        assert distinctive, f"{t.id} has no non-structural distinctive required need (would cross-surface)"


# ══ the families ground their whole domain-shaped catalogs ═══════════════════════════════════════════
def test_markets_family_grounds_on_a_trading_catalog(db):
    _build(db, MKT_SOURCE, _MKT_CATALOG)
    grounded = ground_all(db, MARKETS_TEMPLATES, catalog_source=MKT_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_MKT_IDS   # the whole risk-family set realizes here


def test_custody_family_grounds_on_a_custody_catalog(db):
    _build(db, CUS_SOURCE, _CUS_CATALOG)
    grounded = ground_all(db, CUSTODY_TEMPLATES, catalog_source=CUS_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_CUS_IDS   # the whole settlement-fail funnel


def test_asset_mgmt_family_grounds_on_an_asset_mgmt_catalog(db):
    _build(db, AM_SOURCE, _AM_CATALOG)
    grounded = ground_all(db, ASSET_MGMT_TEMPLATES, catalog_source=AM_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_AM_IDS    # the whole redemption funnel


def test_position_var_risk_grounds_to_var_asof_book(db):
    _build(db, MKT_SOURCE, _MKT_CATALOG)
    gf = ground_template(db, MKT["position_var_risk"], catalog_source=MKT_SOURCE)
    assert isinstance(gf, GroundedFeature)
    assert gf.name == "position_var_risk_90d"             # id + default window (first allowed = 90)
    assert gf.grain_table == "positions"                  # bound on the book entity (positions table)
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.risk.var_1d" in refs                   # the var quantile (distinctive anchor)
    assert "public.positions.book_id" in refs             # the book entity
    assert gf.additivity == "non_additive"                # a VaR quantile is never summed across books
    assert "point-in-time market / counterparty-risk STATE" in gf.pit


def test_counterparty_exposure_grounds_to_epe_netting_set(db):
    _build(db, MKT_SOURCE, _MKT_CATALOG)
    gf = ground_template(db, MKT["counterparty_exposure_trend"], catalog_source=MKT_SOURCE)
    assert gf is not None and gf.name == "counterparty_exposure_trend_180d"
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.xva.epe" in refs                       # the EPE anchor
    assert "public.positions.netting_set_id" in refs      # the netting-set entity


def test_net_fund_flow_grounds_to_fund_flow_never_redeemed(db):
    _build(db, AM_SOURCE, _AM_CATALOG)
    gf = ground_template(db, AM["net_fund_flow_trend"], catalog_source=AM_SOURCE)
    assert gf is not None and gf.name == "net_fund_flow_trend_90d"
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.flows.net_flow" in refs                # the fund_flow anchor (the SAFE pre-signal)
    assert "public.funds.redeemed" not in refs            # the 'redeemed' label is NEVER an input
    assert gf.additivity == "additive"                    # a net flow is an additive flow


# ══ SAFETY BY CONSTRUCTION — the headline: the engine NEVER binds the leakage anchor or protected ════
def test_markets_never_binds_the_target_or_protected_columns(db):
    _build(db, MKT_SOURCE, _MKT_CATALOG)
    grounded = ground_all(db, MARKETS_TEMPLATES, catalog_source=MKT_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _MKT_DANGEROUS)                # default_flag + protected attr never bound


def test_settlement_fail_prediction_never_reads_settlement_fail(db):
    # THE headline custody safety test: a settlement-fail predictor is built from PRE-fail signals
    # (settlement_status / settlement_cycle), NEVER the settlement_fail outcome (a leakage anchor).
    _build(db, CUS_SOURCE, _CUS_CATALOG)
    grounded = ground_all(db, CUSTODY_TEMPLATES, catalog_source=CUS_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert "public.instructions.failed" not in all_refs   # the settlement_fail label is NEVER bound
    assert not (all_refs & _CUS_DANGEROUS)                # leakage + protected never bound
    # …but the PRE-fail signal IS bound — the fail rate is built from settlement_status history.
    gf = ground_template(db, CUS["settlement_fail_rate"], catalog_source=CUS_SOURCE)
    fail_refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.instructions.settlement_status" in fail_refs
    assert "public.instructions.failed" not in fail_refs


def test_redemption_never_reads_redeemed(db):
    # THE headline asset-mgmt safety test: a redemption recipe is built from fund_flow / performance /
    # tracking-error PRE-signals, NEVER the 'redeemed' outcome (a leakage anchor).
    _build(db, AM_SOURCE, _AM_CATALOG)
    grounded = ground_all(db, ASSET_MGMT_TEMPLATES, catalog_source=AM_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert "public.funds.redeemed" not in all_refs        # the 'redeemed' label is NEVER bound
    assert not (all_refs & _AM_DANGEROUS)


# ══ near-label recipes carry near_label True (the 3-part leakage control must flag them) ═════════════
def test_near_label_breadth_recipes_are_flagged(db):
    _build(db, MKT_SOURCE, _MKT_CATALOG)
    _build(db, CUS_SOURCE, _CUS_CATALOG)
    _build(db, AM_SOURCE, _AM_CATALOG)
    # markets: only the counterparty-deterioration EWI borders the close-out/default tail.
    assert {t.id for t in MARKETS_TEMPLATES if t.near_label} == {"counterparty_deterioration_ewi"}
    # custody: the fail RATE and the POST-fail fail-ageing border the settlement-fail outcome.
    assert {t.id for t in CUSTODY_TEMPLATES if t.near_label} == {
        "settlement_fail_rate", "fail_ageing_buckets"}
    # asset-mgmt: the two mandate-compliance breach-proximity recipes border the breach label.
    assert {t.id for t in ASSET_MGMT_TEMPLATES if t.near_label} == {
        "tracking_error_breach_proximity", "mandate_breach_proximity"}
    for fam, src, ids in ((MKT, MKT_SOURCE, {"counterparty_deterioration_ewi"}),
                          (CUS, CUS_SOURCE, {"settlement_fail_rate", "fail_ageing_buckets"}),
                          (AM, AM_SOURCE, {"tracking_error_breach_proximity", "mandate_breach_proximity"})):
        for tid in ids:
            gf = ground_template(db, fam[tid], catalog_source=src)
            assert gf is not None and gf.near_label is True
            assert "NEAR-LABEL" in gf.eligibility          # the ⚠ pre-outcome note travels onto the candidate


def test_markets_and_custody_and_am_non_near_label_recipes_are_not_flagged():
    # the point-in-time market-risk measures + the pre-fail custody signals + the redemption/flow
    # signals are NOT near-label (they do not border their funnel outcome).
    for tid in ("position_var_risk", "notional_netting_exposure", "trading_limit_utilisation"):
        assert MKT[tid].near_label is False
    for tid in ("pre_settlement_aging", "corporate_action_complexity", "sec_lending_utilisation"):
        assert CUS[tid].near_label is False
    for tid in ("net_fund_flow_trend", "performance_vs_benchmark", "aum_stability"):
        assert AM[tid].near_label is False


# ══ a required distinctive need absent -> the recipe SKIPS (degrade path) ════════════════════════════
def test_recipe_skips_when_its_distinctive_concept_is_absent(db):
    # a trading catalog with ONLY var (+ as_of/book/instrument grain) grounds the var recipe but nothing
    # that needs pv01 / notional / expected_exposure / margin / limit / benchmark_rate / watchlist.
    catalog = [
        (CanonicalRow("mini", "p", "instrument_id", "integer", is_grain=True, entity="Instrument"),
         "instrument_id"),
        (CanonicalRow("mini", "p", "book_id", "integer", entity="Book"), "book_id"),
        (CanonicalRow("mini", "p", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("mini", "p", "var_1d", "numeric"), "var"),
    ]
    _build(db, "mini", catalog)
    grounded = {gf.template_id for gf in ground_all(db, MARKETS_TEMPLATES, catalog_source="mini")}
    assert grounded == {"position_var_risk"}              # only the var-anchored recipe grounds
    assert "notional_netting_exposure" not in grounded
    assert "counterparty_exposure_trend" not in grounded


# ══ ROUTING: none of the three families cross-surface on a churn catalog (the key guard) ════════════
def test_breadth_families_do_not_ground_on_a_churn_catalog(db):
    _churn_catalog(db)                                    # the pilot churn catalog — no breadth concepts
    for family in (MARKETS_TEMPLATES, CUSTODY_TEMPLATES, ASSET_MGMT_TEMPLATES):
        grounded = ground_all(db, family, catalog_source=CHURN_SOURCE, roles=("pii_reader",))
        assert grounded == []                             # grounding routed the whole family away


def test_all_templates_on_a_churn_catalog_still_yields_only_the_churn_lens(db):
    _churn_catalog(db)
    churn_only = {gf.template_id for gf in
                  ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    combined = {gf.template_id for gf in
                ground_all(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    assert combined == churn_only                         # ALL_TEMPLATES adds no breadth ids on churn
    assert not (combined & (_ALL_MKT_IDS | _ALL_CUS_IDS | _ALL_AM_IDS))
