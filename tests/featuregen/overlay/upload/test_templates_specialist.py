"""Phase-3 Pass-5 specialist-lines families — the §B9 insurance/bancassurance LAPSE funnel + CLAIMS-FRAUD
journey, the §B13 Islamic-banking conventional funnels + SHARIA-COMPLIANCE overlay, and the §B11 ESG /
sustainable-finance scoring + TRANSITION-RISK journey, authored to Part-F depth.

Three domain-shaped mini-catalogs are built via ``build_graph``:
  • an INSURANCE catalog (policy grain + customer entity · premium · surrender_value · claim_reserve ·
    sum_assured · reinsurance_recoverable · mortality_morbidity · a policy-loan monetary_stock ·
    scheduled_amount · product_type · effective_date · as_of · event_ts),
  • an ISLAMIC catalog (customer grain · profit_rate · benchmark_rate · profit_share_ratio ·
    purification_amount · prohibited_activity_exposure · sukuk · takaful_contribution · a balance
    monetary_stock · a monetary_flow · scheduled_amount · as_of · event_ts), and
  • an ESG catalog (counterparty grain · scope_1/2/3_emissions · financed_emissions · carbon_intensity ·
    emissions_data_quality · taxonomy_alignment · transition_alignment · physical_hazard_score · sll_kpi ·
    geographic · an exposure monetary_stock · as_of),
each carrying DANGEROUS columns — a leakage anchor (``lapsed`` + ``surrendered`` for insurance,
``outcome_label`` for islamic/esg) and a ``protected_attribute`` — to prove the engine NEVER binds them.
Grounding is deterministic (no LLM), so these are exact assertions. The headline SAFETY test: a
lapse/surrender prediction must NOT read ``lapsed`` / ``surrendered`` — it is built from PRE-lapse signals
(premium-payment irregularity, surrender-value trend) instead.

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
    ESG_TEMPLATES,
    INSURANCE_TEMPLATES,
    ISLAMIC_TEMPLATES,
    RETAIL_CHURN_TEMPLATES,
    GroundedFeature,
    ground_all,
    ground_template,
)


def _by_id(templates):
    return {t.id: t for t in templates}


INS = _by_id(INSURANCE_TEMPLATES)
ISL = _by_id(ISLAMIC_TEMPLATES)
ESG = _by_id(ESG_TEMPLATES)


def _build(db, source, catalog):
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog if c}
    build_graph(db, source, rows, concepts=concepts)


# ── insurance catalog — the lapse funnel + claims-fraud journey, at policy grain ─────────────────────
INS_SOURCE = "insurance"
_INS_CATALOG = [
    # policy grain + the policyholder (customer) entity for bancassurance
    (CanonicalRow(INS_SOURCE, "policies", "policy_id", "integer", is_grain=True, entity="Policy"),
     "policy_id"),
    (CanonicalRow(INS_SOURCE, "policyholders", "customer_id", "integer", entity="Customer"),
     "customer_id"),
    (CanonicalRow(INS_SOURCE, "policies", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(INS_SOURCE, "policies", "premium_ts", "timestamp"), "event_timestamp"),
    (CanonicalRow(INS_SOURCE, "policies", "inception_dt", "date"), "effective_date"),
    # insurance-distinctive, non-structural anchors (built from these, NEVER 'lapsed'/'surrendered')
    (CanonicalRow(INS_SOURCE, "policies", "premium", "numeric", additivity="additive", currency="USD"),
     "premium"),
    (CanonicalRow(INS_SOURCE, "policies", "surrender_value", "numeric", additivity="semi_additive",
                  currency="USD"), "surrender_value"),
    (CanonicalRow(INS_SOURCE, "policies", "claim_reserve", "numeric", additivity="semi_additive",
                  currency="USD"), "claim_reserve"),
    (CanonicalRow(INS_SOURCE, "policies", "sum_assured", "numeric", additivity="semi_additive",
                  currency="USD"), "sum_assured"),
    (CanonicalRow(INS_SOURCE, "policies", "reinsurance_recoverable", "numeric",
                  additivity="semi_additive", currency="USD"), "reinsurance_recoverable"),
    (CanonicalRow(INS_SOURCE, "policies", "mortality_rate", "numeric"), "mortality_morbidity"),
    # supporting: policy-loan balance, premium-due schedule, banking product, income proxy
    (CanonicalRow(INS_SOURCE, "policies", "policy_loan", "numeric", additivity="semi_additive",
                  currency="USD"), "monetary_stock"),
    (CanonicalRow(INS_SOURCE, "policies", "premium_due", "numeric", additivity="additive",
                  currency="USD"), "scheduled_amount"),
    (CanonicalRow(INS_SOURCE, "policyholders", "banking_product", "text"), "product_type"),
    (CanonicalRow(INS_SOURCE, "policyholders", "income", "numeric", additivity="additive",
                  currency="USD"), "monetary_flow"),
    # DANGEROUS — never a feature input (lapsed/surrendered = the specialist leakage anchors)
    (CanonicalRow(INS_SOURCE, "policies", "lapsed", "boolean"), "lapsed"),                 # leakage
    (CanonicalRow(INS_SOURCE, "policies", "surrendered", "boolean"), "surrendered"),       # leakage
    (CanonicalRow(INS_SOURCE, "policyholders", "age_band", "text"), "protected_attribute"),  # protected
]
_INS_DANGEROUS = {"public.policies.lapsed", "public.policies.surrendered",
                  "public.policyholders.age_band"}
_ALL_INS_IDS = {t.id for t in INSURANCE_TEMPLATES}


# ── islamic catalog — conventional funnels reframed + the sharia-compliance overlay, at customer grain ─
ISL_SOURCE = "islamic"
_ISL_CATALOG = [
    (CanonicalRow(ISL_SOURCE, "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(ISL_SOURCE, "accounts", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(ISL_SOURCE, "txns", "txn_ts", "timestamp"), "event_timestamp"),
    # islamic-distinctive, non-structural anchors (profit_rate is NOT interest)
    (CanonicalRow(ISL_SOURCE, "accounts", "profit_rate", "numeric"), "profit_rate"),
    (CanonicalRow(ISL_SOURCE, "accounts", "sonia", "numeric"), "benchmark_rate"),
    (CanonicalRow(ISL_SOURCE, "accounts", "profit_share_ratio", "numeric"), "profit_share_ratio"),
    (CanonicalRow(ISL_SOURCE, "accounts", "prohibited_exposure", "numeric", additivity="semi_additive",
                  currency="USD"), "prohibited_activity_exposure"),
    (CanonicalRow(ISL_SOURCE, "accounts", "sukuk_class", "text"), "sukuk"),
    (CanonicalRow(ISL_SOURCE, "accounts", "balance", "numeric", additivity="semi_additive",
                  currency="USD"), "monetary_stock"),
    (CanonicalRow(ISL_SOURCE, "txns", "purification", "numeric", additivity="additive", currency="USD"),
     "purification_amount"),
    (CanonicalRow(ISL_SOURCE, "txns", "takaful", "numeric", additivity="additive", currency="USD"),
     "takaful_contribution"),
    (CanonicalRow(ISL_SOURCE, "txns", "income", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(ISL_SOURCE, "txns", "installment_due", "numeric", additivity="additive",
                  currency="USD"), "scheduled_amount"),
    # DANGEROUS — never a feature input
    (CanonicalRow(ISL_SOURCE, "accounts", "churned", "boolean"), "outcome_label"),         # leakage
    (CanonicalRow(ISL_SOURCE, "accounts", "age_band", "text"), "protected_attribute"),     # protected
]
_ISL_DANGEROUS = {"public.accounts.churned", "public.accounts.age_band"}
_ALL_ISL_IDS = {t.id for t in ISLAMIC_TEMPLATES}


# ── esg catalog — scoring + transition-risk journey, at counterparty (financed-entity) grain ─────────
ESG_SOURCE = "esg"
_ESG_CATALOG = [
    (CanonicalRow(ESG_SOURCE, "entities", "counterparty_id", "integer", is_grain=True,
                  entity="Counterparty"), "counterparty_id"),
    (CanonicalRow(ESG_SOURCE, "entities", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(ESG_SOURCE, "entities", "region", "text"), "geographic"),
    (CanonicalRow(ESG_SOURCE, "entities", "exposure", "numeric", additivity="semi_additive",
                  currency="USD"), "monetary_stock"),
    # esg-distinctive, non-structural anchors
    (CanonicalRow(ESG_SOURCE, "emissions", "scope_1", "numeric", additivity="additive"),
     "scope_1_emissions"),
    (CanonicalRow(ESG_SOURCE, "emissions", "scope_2", "numeric", additivity="additive"),
     "scope_2_emissions"),
    (CanonicalRow(ESG_SOURCE, "emissions", "scope_3", "numeric", additivity="additive"),
     "scope_3_emissions"),
    (CanonicalRow(ESG_SOURCE, "emissions", "financed", "numeric", additivity="additive"),
     "financed_emissions"),
    (CanonicalRow(ESG_SOURCE, "emissions", "intensity", "numeric"), "carbon_intensity"),
    (CanonicalRow(ESG_SOURCE, "emissions", "pcaf_dq", "numeric"), "emissions_data_quality"),
    (CanonicalRow(ESG_SOURCE, "transition", "taxonomy", "numeric"), "taxonomy_alignment"),
    (CanonicalRow(ESG_SOURCE, "transition", "alignment", "numeric"), "transition_alignment"),
    (CanonicalRow(ESG_SOURCE, "transition", "hazard", "numeric"), "physical_hazard_score"),
    (CanonicalRow(ESG_SOURCE, "transition", "sll_kpi", "numeric"), "sll_kpi"),
    # DANGEROUS — never a feature input
    (CanonicalRow(ESG_SOURCE, "entities", "esg_outcome", "boolean"), "outcome_label"),        # leakage
    (CanonicalRow(ESG_SOURCE, "entities", "borrower_age_band", "text"), "protected_attribute"),  # prot
]
_ESG_DANGEROUS = {"public.entities.esg_outcome", "public.entities.borrower_age_band"}
_ALL_ESG_IDS = {t.id for t in ESG_TEMPLATES}


# ══ authored the three families (insurance 10 · islamic 8 · esg 9) ════════════════════════════════════
def test_specialist_families_authored():
    assert len(INSURANCE_TEMPLATES) == 10
    assert len(ISLAMIC_TEMPLATES) == 8
    assert len(ESG_TEMPLATES) == 9
    assert set(INS) == {
        "premium_payment_irregularity", "missed_premium_streak", "surrender_value_trajectory",
        "policy_loan_utilisation", "claims_frequency_severity", "claims_fraud_typology",
        "reinsurance_recoverable_concentration", "sum_assured_adequacy", "bancassurance_cross_hold",
        "mortality_morbidity_loading"}
    assert set(ISL) == {
        "profit_rate_exposure", "profit_sharing_split_behaviour", "purification_ratio",
        "prohibited_activity_exposure_share", "sukuk_concentration", "takaful_contribution_behaviour",
        "islamic_deposit_beta", "murabaha_installment_behaviour"}
    assert set(ESG) == {
        "emissions_trend_by_scope", "carbon_intensity_trajectory", "financed_emissions_attribution",
        "emissions_data_quality_reliance", "taxonomy_alignment_share", "transition_alignment_gap",
        "physical_hazard_exposure", "sll_kpi_achievement", "scope3_value_chain_exposure"}
    # every need references a real concept (also enforced at import by _validate_registry)
    for t in INSURANCE_TEMPLATES + ISLAMIC_TEMPLATES + ESG_TEMPLATES:
        for need in t.needs:
            assert need.concept in CONCEPT_REGISTRY


# ══ ROUTING (the locked invariant): every recipe anchors on a NON-STRUCTURAL distinctive concept ═════
def test_every_specialist_recipe_anchors_on_a_non_structural_distinctive_concept():
    # A recipe must REQUIRE at least one concept that is (a) outside the churn catalog's vocabulary AND
    # (b) non-structural (not an entity/as_of concept — those get structural is_grain/is_as_of credit in
    # _match and would bind a churn grain/as-of column, cross-surfacing the family). This is the precise
    # condition the runtime routing test below enforces.
    churn_concepts = {c for _r, c in _CHURN_CATALOG if c}
    for t in INSURANCE_TEMPLATES + ISLAMIC_TEMPLATES + ESG_TEMPLATES:
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
def test_insurance_family_grounds_on_an_insurance_catalog(db):
    _build(db, INS_SOURCE, _INS_CATALOG)
    grounded = ground_all(db, INSURANCE_TEMPLATES, catalog_source=INS_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_INS_IDS   # the whole lapse + claims set realizes


def test_islamic_family_grounds_on_an_islamic_catalog(db):
    _build(db, ISL_SOURCE, _ISL_CATALOG)
    grounded = ground_all(db, ISLAMIC_TEMPLATES, catalog_source=ISL_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_ISL_IDS   # the whole sharia overlay + funnels


def test_esg_family_grounds_on_an_esg_catalog(db):
    _build(db, ESG_SOURCE, _ESG_CATALOG)
    grounded = ground_all(db, ESG_TEMPLATES, catalog_source=ESG_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_ESG_IDS   # the whole scoring + transition journey


def test_premium_irregularity_grounds_to_premium_event_policy(db):
    _build(db, INS_SOURCE, _INS_CATALOG)
    gf = ground_template(db, INS["premium_payment_irregularity"], catalog_source=INS_SOURCE)
    assert isinstance(gf, GroundedFeature)
    assert gf.name == "premium_payment_irregularity_365d"     # id + default window (first allowed = 365)
    assert gf.grain_table == "policies"                       # bound on the policy grain
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.policies.premium" in refs                  # the premium anchor (the SAFE pre-signal)
    assert "public.policies.lapsed" not in refs               # the 'lapsed' label is NEVER an input
    assert "public.policies.surrendered" not in refs          # nor 'surrendered'
    assert gf.additivity == "n/a" and gf.near_label is False


def test_surrender_trajectory_grounds_to_surrender_value_never_surrendered(db):
    _build(db, INS_SOURCE, _INS_CATALOG)
    gf = ground_template(db, INS["surrender_value_trajectory"], catalog_source=INS_SOURCE)
    assert gf is not None and gf.name == "surrender_value_trajectory_365d"
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.policies.surrender_value" in refs          # the surrender_value anchor (PRE-surrender)
    assert "public.policies.surrendered" not in refs          # the 'surrendered' label is NEVER an input
    assert gf.additivity == "non_additive"                    # a surrender-pressure ratio
    assert "point-in-time policy STATE" in gf.pit


def test_profit_rate_exposure_grounds_to_profit_rate_never_interest(db):
    _build(db, ISL_SOURCE, _ISL_CATALOG)
    gf = ground_template(db, ISL["profit_rate_exposure"], catalog_source=ISL_SOURCE)
    assert gf is not None and gf.name == "profit_rate_exposure_365d"
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.accounts.profit_rate" in refs              # the Islamic profit-rate anchor (not riba)
    assert gf.additivity == "non_additive"                    # a rate


def test_financed_emissions_grounds_additive_pcaf_attributed(db):
    _build(db, ESG_SOURCE, _ESG_CATALOG)
    gf = ground_template(db, ESG["financed_emissions_attribution"], catalog_source=ESG_SOURCE)
    assert gf is not None and gf.name == "financed_emissions_attribution_365d"
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.emissions.financed" in refs                # the PCAF financed-emissions anchor
    assert gf.additivity == "additive"                        # attributed -> additive across the book


# ══ SAFETY BY CONSTRUCTION — the headline: the engine NEVER binds the leakage anchor or protected ════
def test_lapse_prediction_never_reads_lapsed_or_surrendered(db):
    # THE headline insurance safety test: a lapse/surrender predictor is built from PRE-lapse signals
    # (premium irregularity, missed-premium streak, surrender-value trend), NEVER the lapsed/surrendered
    # outcomes (leakage anchors the engine refuses).
    _build(db, INS_SOURCE, _INS_CATALOG)
    grounded = ground_all(db, INSURANCE_TEMPLATES, catalog_source=INS_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert "public.policies.lapsed" not in all_refs           # the lapsed label is NEVER bound
    assert "public.policies.surrendered" not in all_refs      # nor surrendered
    assert not (all_refs & _INS_DANGEROUS)                    # leakage + protected never bound
    # …but the PRE-lapse signals ARE bound — the funnel is built from premium/surrender behaviour.
    assert "public.policies.premium" in all_refs
    assert "public.policies.surrender_value" in all_refs


def test_islamic_never_binds_the_target_or_protected_columns(db):
    _build(db, ISL_SOURCE, _ISL_CATALOG)
    grounded = ground_all(db, ISLAMIC_TEMPLATES, catalog_source=ISL_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _ISL_DANGEROUS)                    # outcome_label + protected never bound


def test_esg_never_binds_the_target_or_protected_columns(db):
    _build(db, ESG_SOURCE, _ESG_CATALOG)
    grounded = ground_all(db, ESG_TEMPLATES, catalog_source=ESG_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _ESG_DANGEROUS)                    # outcome_label + protected never bound


def test_mortality_morbidity_binds_the_rate_never_a_special_category_column(db):
    # mortality_morbidity is the actuarial RATE (bindable, public); the recipe carries the special-
    # category consent note. A health-STATUS special_category column is engine-blocked by construction.
    _build(db, INS_SOURCE, _INS_CATALOG)
    gf = ground_template(db, INS["mortality_morbidity_loading"], catalog_source=INS_SOURCE)
    assert gf is not None
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.policies.mortality_rate" in refs           # the actuarial RATE anchor
    assert "special_category" in gf.eligibility or "HEALTH-ADJACENT" in gf.eligibility


# ══ near-label recipes carry near_label True (the 3-part leakage control must flag them) ═════════════
def test_near_label_specialist_recipes_are_flagged(db):
    _build(db, INS_SOURCE, _INS_CATALOG)
    _build(db, ISL_SOURCE, _ISL_CATALOG)
    _build(db, ESG_SOURCE, _ESG_CATALOG)
    # insurance: only the claims-fraud typology borders the SIU/confirmed-fraud label.
    assert {t.id for t in INSURANCE_TEMPLATES if t.near_label} == {"claims_fraud_typology"}
    # islamic: only the prohibited-activity exposure share borders the compliance-breach determination.
    assert {t.id for t in ISLAMIC_TEMPLATES if t.near_label} == {"prohibited_activity_exposure_share"}
    # esg: an ESG/climate signal does not border a customer outcome -> no near-label recipe.
    assert {t.id for t in ESG_TEMPLATES if t.near_label} == set()
    for fam, src, ids in ((INS, INS_SOURCE, {"claims_fraud_typology"}),
                          (ISL, ISL_SOURCE, {"prohibited_activity_exposure_share"})):
        for tid in ids:
            gf = ground_template(db, fam[tid], catalog_source=src)
            assert gf is not None and gf.near_label is True
            assert "NEAR-LABEL" in gf.eligibility          # the ⚠ pre-outcome note travels onto the candidate


def test_specialist_non_near_label_recipes_are_not_flagged():
    # the persistency + claims-severity + underwriting + reinsurance/bancassurance signals, the profit-
    # rate / sukuk / takaful signals and every ESG scoring/transition signal do not border their outcome.
    for tid in ("premium_payment_irregularity", "surrender_value_trajectory", "claims_frequency_severity",
                "sum_assured_adequacy", "bancassurance_cross_hold"):
        assert INS[tid].near_label is False
    for tid in ("profit_rate_exposure", "sukuk_concentration", "takaful_contribution_behaviour"):
        assert ISL[tid].near_label is False
    for tid in ("emissions_trend_by_scope", "carbon_intensity_trajectory", "sll_kpi_achievement"):
        assert ESG[tid].near_label is False


# ══ the ESG cross-scope recipe carries the additivity double-count guard ═════════════════════════════
def test_esg_cross_scope_recipe_carries_the_additivity_double_count_guard(db):
    _build(db, ESG_SOURCE, _ESG_CATALOG)
    gf = ground_template(db, ESG["emissions_trend_by_scope"], catalog_source=ESG_SOURCE)
    assert gf is not None
    assert gf.additivity == "additive"                        # per-scope absolute is additive WITHIN scope
    # the cross-scope / value-chain double-count trap is annotated in notes (the honest additivity guard).
    assert any("DOUBLE-COUNT" in n.upper() for n in gf.notes)
    assert any("scope" in n.lower() for n in gf.notes)
    # …and the scope-3-specific recipe carries the cross-ENTITY (portfolio) double-count guard too.
    gf3 = ground_template(db, ESG["scope3_value_chain_exposure"], catalog_source=ESG_SOURCE)
    assert gf3 is not None and gf3.additivity == "additive"
    assert any("DOUBLE-COUNT" in n.upper() or "PORTFOLIO" in n.upper() for n in gf3.notes)


# ══ a required distinctive need absent -> the recipe SKIPS (degrade path) ════════════════════════════
def test_recipe_skips_when_its_distinctive_concept_is_absent(db):
    # an insurance catalog with ONLY premium (+ event_ts / policy grain) grounds the premium recipes but
    # nothing that needs surrender_value / claim_reserve / sum_assured / reinsurance / mortality.
    catalog = [
        (CanonicalRow("mini", "p", "policy_id", "integer", is_grain=True, entity="Policy"), "policy_id"),
        (CanonicalRow("mini", "p", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("mini", "p", "premium_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("mini", "p", "premium", "numeric", additivity="additive", currency="USD"),
         "premium"),
    ]
    _build(db, "mini", catalog)
    grounded = {gf.template_id for gf in ground_all(db, INSURANCE_TEMPLATES, catalog_source="mini")}
    assert "premium_payment_irregularity" in grounded         # the premium-anchored recipe grounds
    assert "missed_premium_streak" in grounded
    assert "surrender_value_trajectory" not in grounded       # no surrender_value -> routed away
    assert "claims_frequency_severity" not in grounded        # no claim_reserve -> routed away
    assert "mortality_morbidity_loading" not in grounded


# ══ ROUTING: none of the three families cross-surface on a churn catalog (the key guard) ════════════
def test_specialist_families_do_not_ground_on_a_churn_catalog(db):
    _churn_catalog(db)                                    # the pilot churn catalog — no specialist concepts
    for family in (INSURANCE_TEMPLATES, ISLAMIC_TEMPLATES, ESG_TEMPLATES):
        grounded = ground_all(db, family, catalog_source=CHURN_SOURCE, roles=("pii_reader",))
        assert grounded == []                             # grounding routed the whole family away


def test_all_templates_on_a_churn_catalog_still_yields_only_the_churn_lens(db):
    _churn_catalog(db)
    churn_only = {gf.template_id for gf in
                  ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    combined = {gf.template_id for gf in
                ground_all(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    assert combined == churn_only                         # ALL_TEMPLATES adds no specialist ids on churn
    assert not (combined & (_ALL_INS_IDS | _ALL_ISL_IDS | _ALL_ESG_IDS))
