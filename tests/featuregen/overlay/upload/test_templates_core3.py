"""Phase-3 Pass-3 core-3 families — the §B6 collections/recoveries journey, the §B7 deposit/liquidity/
treasury (ALM) stability spectrum and the §B14 payments-as-a-business set, authored to Part-F depth.

Three domain-shaped mini-catalogs are built via ``build_graph``:
  • a COLLECTIONS catalog (delinquency_bucket / dpd / scheduled_amount / cost_to_collect / restructured
    / recovery_amount / write_off_amount + flow/stock/event) at customer grain,
  • a TREASURY/ALM catalog (benchmark_rate / ftp_rate / wholesale_funding / maturity_date / tenor /
    hqla / lcr / nsfr / repricing_gap / beta + a balance stock), and
  • a PAYMENTS catalog (payment_rail / scheme / interchange / merchant_discount_rate / settlement_status
    / settlement_cycle / direct_debit / standing_order / corridor / country_code / iso20022_purpose_code
    + flow/event),
each carrying two DANGEROUS columns — a leakage anchor (``default_flag`` / ``outcome_label`` /
``fraud_flag``) and a ``protected_attribute`` — to prove the engine NEVER binds them. Grounding is
deterministic (no LLM), so these are exact assertions.

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
    AML_TEMPLATES,
    COLLECTIONS_TEMPLATES,
    CREDIT_RISK_TEMPLATES,
    DEPOSITS_TEMPLATES,
    FRAUD_TEMPLATES,
    PAYMENTS_TEMPLATES,
    RETAIL_CHURN_TEMPLATES,
    GroundedFeature,
    ground_all,
    ground_template,
)


def _by_id(templates):
    return {t.id: t for t in templates}


COLL = _by_id(COLLECTIONS_TEMPLATES)
DEP = _by_id(DEPOSITS_TEMPLATES)
PAY = _by_id(PAYMENTS_TEMPLATES)


def _build(db, source, catalog):
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog if c}
    build_graph(db, source, rows, concepts=concepts)


# ── collections catalog — delinquency → recovery journey, at customer grain ─────────────────────────
COLL_SOURCE = "collections"
_COLL_CATALOG = [
    (CanonicalRow(COLL_SOURCE, "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(COLL_SOURCE, "accounts", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(COLL_SOURCE, "accounts", "balance", "numeric", additivity="semi_additive",
                  currency="USD"), "monetary_stock"),
    # arrears / roll dynamics (near-label)
    (CanonicalRow(COLL_SOURCE, "accounts", "delinquency_bucket", "text"), "delinquency_bucket"),
    (CanonicalRow(COLL_SOURCE, "accounts", "dpd", "integer"), "dpd"),
    (CanonicalRow(COLL_SOURCE, "accounts", "restructured", "boolean"), "restructured_flag"),
    # arrangement + cost + recovery/charge-off
    (CanonicalRow(COLL_SOURCE, "payments", "scheduled_amount", "numeric", additivity="additive",
                  currency="USD"), "scheduled_amount"),
    (CanonicalRow(COLL_SOURCE, "payments", "amount", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(COLL_SOURCE, "payments", "payment_ts", "timestamp"), "event_timestamp"),
    (CanonicalRow(COLL_SOURCE, "workout", "cost_to_collect", "numeric", additivity="additive",
                  currency="USD"), "cost_to_collect"),
    (CanonicalRow(COLL_SOURCE, "workout", "recovery_amount", "numeric", additivity="additive",
                  currency="USD"), "recovery_amount"),
    (CanonicalRow(COLL_SOURCE, "workout", "write_off_amount", "numeric", additivity="additive",
                  currency="USD"), "write_off_amount"),
    # DANGEROUS — never a feature input (deliberately NOT is_grain / is_as_of)
    (CanonicalRow(COLL_SOURCE, "accounts", "charged_off", "boolean"), "outcome_label"),        # leakage
    (CanonicalRow(COLL_SOURCE, "accounts", "age_band", "text"), "protected_attribute"),        # protected
]
_COLL_DANGEROUS = {"public.accounts.charged_off", "public.accounts.age_band"}
_ALL_COLL_IDS = {t.id for t in COLLECTIONS_TEMPLATES}


# ── treasury / ALM catalog — the stability spectrum, at depositor grain ──────────────────────────────
TREASURY_SOURCE = "treasury"
_TREASURY_CATALOG = [
    (CanonicalRow(TREASURY_SOURCE, "deposits", "customer_id", "integer", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "balance", "numeric", additivity="semi_additive",
                  currency="USD"), "monetary_stock"),
    # ALM-distinctive treasury anchors (every recipe requires one)
    (CanonicalRow(TREASURY_SOURCE, "deposits", "benchmark_rate", "numeric"), "benchmark_rate"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "ftp_rate", "numeric"), "ftp_rate"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "wholesale_funding", "numeric",
                  additivity="semi_additive", currency="USD"), "wholesale_funding"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "maturity_dt", "date"), "maturity_date"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "term", "integer"), "tenor"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "hqla", "numeric", additivity="semi_additive",
                  currency="USD"), "hqla"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "lcr", "numeric"), "lcr"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "nsfr", "numeric"), "nsfr"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "repricing_gap", "numeric"), "repricing_gap"),
    (CanonicalRow(TREASURY_SOURCE, "deposits", "deposit_beta", "numeric"), "beta"),
    # DANGEROUS — never a feature input
    (CanonicalRow(TREASURY_SOURCE, "deposits", "closed_flag", "boolean"), "outcome_label"),   # leakage
    (CanonicalRow(TREASURY_SOURCE, "deposits", "gender", "text"), "protected_attribute"),     # protected
]
_TREASURY_DANGEROUS = {"public.deposits.closed_flag", "public.deposits.gender"}
_ALL_DEP_IDS = {t.id for t in DEPOSITS_TEMPLATES}


# ── payments catalog — payments-as-a-business, at customer grain ─────────────────────────────────────
PAY_SOURCE = "payments_biz"
_PAY_CATALOG = [
    (CanonicalRow(PAY_SOURCE, "customers", "customer_id", "integer", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(PAY_SOURCE, "txns", "amount", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(PAY_SOURCE, "txns", "txn_ts", "timestamp"), "event_timestamp"),
    # payments-distinctive anchors
    (CanonicalRow(PAY_SOURCE, "txns", "rail", "text"), "payment_rail"),
    (CanonicalRow(PAY_SOURCE, "txns", "scheme", "text"), "scheme"),
    (CanonicalRow(PAY_SOURCE, "txns", "interchange", "numeric", additivity="additive", currency="USD"),
     "interchange"),
    (CanonicalRow(PAY_SOURCE, "txns", "mdr", "numeric"), "merchant_discount_rate"),
    (CanonicalRow(PAY_SOURCE, "txns", "settlement_status", "text"), "settlement_status"),
    (CanonicalRow(PAY_SOURCE, "txns", "settlement_cycle", "text"), "settlement_cycle"),
    (CanonicalRow(PAY_SOURCE, "txns", "direct_debit", "text"), "direct_debit"),
    (CanonicalRow(PAY_SOURCE, "txns", "standing_order", "text"), "standing_order"),
    (CanonicalRow(PAY_SOURCE, "txns", "corridor", "text"), "corridor"),
    (CanonicalRow(PAY_SOURCE, "txns", "country_code", "text"), "country_code"),
    (CanonicalRow(PAY_SOURCE, "txns", "purpose", "text"), "iso20022_purpose_code"),
    # DANGEROUS — never a feature input
    (CanonicalRow(PAY_SOURCE, "txns", "fraud_flag", "boolean"), "fraud_flag"),                # leakage
    (CanonicalRow(PAY_SOURCE, "customers", "age_band", "text"), "protected_attribute"),       # protected
]
_PAY_DANGEROUS = {"public.txns.fraud_flag", "public.customers.age_band"}
_ALL_PAY_IDS = {t.id for t in PAYMENTS_TEMPLATES}


# ══ authored the three families (10 each, across the journey) ═══════════════════════════════════════
def test_core3_families_authored():
    assert len(COLLECTIONS_TEMPLATES) == 10
    assert len(DEPOSITS_TEMPLATES) == 10
    assert len(PAYMENTS_TEMPLATES) == 10
    assert set(COLL) == {
        "promise_to_pay_adherence", "payment_plan_adherence", "cure_reage_dynamics",
        "roll_forward_severity", "right_party_contact_intensity", "days_in_collection",
        "hardship_forbearance_in_collection", "cost_to_collect_ratio", "recovery_rate",
        "write_off_severity"}
    assert set(DEP) == {
        "nmd_stickiness", "hqla_eligibility_contribution", "nsfr_asf_contribution", "deposit_beta",
        "lcr_outflow_weight", "repricing_gap_exposure", "hot_money_share",
        "rate_sensitive_concentration", "maturity_ladder_runoff", "early_withdrawal_break"}
    assert set(PAY) == {
        "rail_volume_value", "rail_scheme_diversity", "purpose_code_diversity", "interchange_revenue",
        "merchant_discount_economics", "authorisation_decline_rate", "chargeback_dispute_rate",
        "return_payment_rate", "settlement_lag", "corridor_cross_border_share"}
    # every need references a real concept (also enforced at import by _validate_registry)
    for t in COLLECTIONS_TEMPLATES + DEPOSITS_TEMPLATES + PAYMENTS_TEMPLATES:
        for need in t.needs:
            assert need.concept in CONCEPT_REGISTRY


# ══ ROUTING (the locked invariant): every recipe anchors on a NON-STRUCTURAL distinctive concept ═════
def test_every_core3_recipe_anchors_on_a_non_structural_distinctive_concept():
    # A recipe must REQUIRE at least one concept that is (a) outside the churn catalog's vocabulary AND
    # (b) non-structural (not an entity/as_of concept — those get structural is_grain/is_as_of credit in
    # _match and would bind a churn grain/as-of column, cross-surfacing the family). This is the precise
    # condition the runtime routing test below enforces.
    churn_concepts = {c for _r, c in _CHURN_CATALOG if c}
    for t in COLLECTIONS_TEMPLATES + DEPOSITS_TEMPLATES + PAYMENTS_TEMPLATES:
        distinctive = False
        for n in t.needs:
            if n.optional:
                continue
            c = CONCEPT_REGISTRY[n.concept]
            structural = c.entity_link is not None or c.pit_role == "as_of"
            if not structural and n.concept not in churn_concepts:
                distinctive = True
        assert distinctive, f"{t.id} has no non-structural distinctive required need (would cross-surface)"


# ══ the families ground on their domain-shaped catalogs ══════════════════════════════════════════════
def test_collections_family_grounds_on_a_collections_catalog(db):
    _build(db, COLL_SOURCE, _COLL_CATALOG)
    grounded = ground_all(db, COLLECTIONS_TEMPLATES, catalog_source=COLL_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_COLL_IDS   # the whole journey realizes here


def test_deposits_family_grounds_on_a_treasury_catalog(db):
    _build(db, TREASURY_SOURCE, _TREASURY_CATALOG)
    grounded = ground_all(db, DEPOSITS_TEMPLATES, catalog_source=TREASURY_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_DEP_IDS    # the whole spectrum realizes here


def test_payments_family_grounds_on_a_payments_catalog(db):
    _build(db, PAY_SOURCE, _PAY_CATALOG)
    grounded = ground_all(db, PAYMENTS_TEMPLATES, catalog_source=PAY_SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_PAY_IDS


def test_promise_to_pay_grounds_to_scheduled_flow_event_customer(db):
    _build(db, COLL_SOURCE, _COLL_CATALOG)
    gf = ground_template(db, COLL["promise_to_pay_adherence"], catalog_source=COLL_SOURCE)
    assert isinstance(gf, GroundedFeature)
    assert gf.name == "promise_to_pay_adherence_90d"      # id + default window (first allowed = 90)
    assert gf.grain_table == "accounts"                   # bound on the customer grain
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.payments.scheduled_amount" in refs     # the scheduled_amount (distinctive anchor)
    assert "public.payments.amount" in refs               # the monetary_flow paid
    assert "public.accounts.customer_id" in refs          # the entity/grain
    assert gf.additivity == "non_additive" and gf.near_label is False


def test_deposit_beta_grounds_to_benchmark_balance_asof_customer(db):
    _build(db, TREASURY_SOURCE, _TREASURY_CATALOG)
    gf = ground_template(db, DEP["deposit_beta"], catalog_source=TREASURY_SOURCE)
    assert gf is not None and gf.name == "deposit_beta_365d"    # id + default window (first allowed = 365)
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.deposits.benchmark_rate" in refs       # the ALM-distinctive reference rate anchor
    assert "public.deposits.balance" in refs              # the monetary_stock
    assert gf.additivity == "non_additive"                # a beta
    assert "point-in-time deposit-behaviour STATE" in gf.pit


def test_interchange_grounds_to_flow_event_customer(db):
    _build(db, PAY_SOURCE, _PAY_CATALOG)
    gf = ground_template(db, PAY["interchange_revenue"], catalog_source=PAY_SOURCE)
    assert gf is not None and gf.name == "interchange_revenue_90d"
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.txns.interchange" in refs              # the interchange economics flow (anchor)
    assert gf.additivity == "additive"                    # an economics flow


# ══ safety by construction — the engine NEVER binds the leakage anchor or a protected column ═════════
def test_collections_never_binds_the_target_or_protected_columns(db):
    _build(db, COLL_SOURCE, _COLL_CATALOG)
    grounded = ground_all(db, COLLECTIONS_TEMPLATES, catalog_source=COLL_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _COLL_DANGEROUS)               # leakage anchor + protected attr never bound


def test_deposits_never_binds_the_target_or_protected_columns(db):
    _build(db, TREASURY_SOURCE, _TREASURY_CATALOG)
    grounded = ground_all(db, DEPOSITS_TEMPLATES, catalog_source=TREASURY_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _TREASURY_DANGEROUS)


def test_payments_never_binds_the_target_or_protected_columns(db):
    _build(db, PAY_SOURCE, _PAY_CATALOG)
    grounded = ground_all(db, PAYMENTS_TEMPLATES, catalog_source=PAY_SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert not (all_refs & _PAY_DANGEROUS)


# ══ near-label recipes carry near_label True (the 3-part leakage control must flag them) ═════════════
def test_near_label_collections_recipes_are_flagged(db):
    _build(db, COLL_SOURCE, _COLL_CATALOG)
    expected_near_label = {
        "cure_reage_dynamics", "roll_forward_severity", "days_in_collection",
        "hardship_forbearance_in_collection", "recovery_rate", "write_off_severity"}
    assert {t.id for t in COLLECTIONS_TEMPLATES if t.near_label} == expected_near_label
    for tid in expected_near_label:
        gf = ground_template(db, COLL[tid], catalog_source=COLL_SOURCE)
        assert gf is not None and gf.near_label is True
        assert "NEAR-LABEL" in gf.eligibility            # the ⚠ pre-outcome note travels onto the candidate


def test_recovery_and_write_off_carry_the_hard_post_default_flag(db):
    _build(db, COLL_SOURCE, _COLL_CATALOG)
    for tid, anchor in (("recovery_rate", "public.workout.recovery_amount"),
                        ("write_off_severity", "public.workout.write_off_amount")):
        gf = ground_template(db, COLL[tid], catalog_source=COLL_SOURCE)
        assert gf is not None and gf.near_label is True
        assert "POST-DEFAULT" in gf.eligibility          # the ⚠⚠ post-charge-off hard flag
        assert anchor in {ref for _src, ref in gf.derives_pairs}   # binds its recovery/write-off anchor
        # the notes state, in hard terms, that the amount must NOT be an input to a cure/recovery model
        assert any("NEVER read" in n or "leak" in n for n in gf.notes)


def test_deposits_and_payments_have_no_near_label_recipes():
    # treasury signals + payments throughput/economics do not border a customer outcome.
    assert {t.id for t in DEPOSITS_TEMPLATES if t.near_label} == set()
    assert {t.id for t in PAYMENTS_TEMPLATES if t.near_label} == set()


# ══ a required distinctive need absent -> the recipe SKIPS (degrade path) ════════════════════════════
def test_recipe_skips_when_its_distinctive_concept_is_absent(db):
    # a treasury catalog with ONLY benchmark_rate (+ balance/as_of/entity) grounds the beta recipe but
    # nothing that needs ftp/hqla/lcr/nsfr/repricing_gap/wholesale/maturity/tenor.
    catalog = [
        (CanonicalRow("mini", "d", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("mini", "d", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("mini", "d", "balance", "numeric", additivity="semi_additive", currency="USD"),
         "monetary_stock"),
        (CanonicalRow("mini", "d", "benchmark_rate", "numeric"), "benchmark_rate"),
    ]
    _build(db, "mini", catalog)
    grounded = {gf.template_id for gf in ground_all(db, DEPOSITS_TEMPLATES, catalog_source="mini")}
    assert grounded == {"deposit_beta"}                   # only the benchmark_rate-anchored recipe grounds
    assert "hqla_eligibility_contribution" not in grounded
    assert "repricing_gap_exposure" not in grounded


# ══ ROUTING: none of the three families cross-surface on a churn catalog (the key guard) ════════════
def test_core3_families_do_not_ground_on_a_churn_catalog(db):
    _churn_catalog(db)                                    # the pilot churn catalog — no core-3 concepts
    for family in (COLLECTIONS_TEMPLATES, DEPOSITS_TEMPLATES, PAYMENTS_TEMPLATES):
        grounded = ground_all(db, family, catalog_source=CHURN_SOURCE, roles=("pii_reader",))
        assert grounded == []                             # grounding routed the whole family away


def test_all_templates_on_a_churn_catalog_still_yields_only_the_churn_lens(db):
    _churn_catalog(db)
    churn_only = {gf.template_id for gf in
                  ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    combined = {gf.template_id for gf in
                ground_all(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    assert combined == churn_only                         # ALL_TEMPLATES adds no core-3 ids on churn
    assert not (combined & (_ALL_COLL_IDS | _ALL_DEP_IDS | _ALL_PAY_IDS))


# ══ registry: ALL_TEMPLATES is the seven-family union, globally id-unique ════════════════════════════
def test_all_templates_registry_is_the_seven_family_union():
    ids = [t.id for t in ALL_TEMPLATES]
    assert len(ids) == len(set(ids))                      # no duplicate id across families
    assert set(ids) == (
        {t.id for t in RETAIL_CHURN_TEMPLATES} | {t.id for t in CREDIT_RISK_TEMPLATES}
        | {t.id for t in FRAUD_TEMPLATES} | {t.id for t in AML_TEMPLATES}
        | _ALL_COLL_IDS | _ALL_DEP_IDS | _ALL_PAY_IDS)
    assert len(ALL_TEMPLATES) == (
        len(RETAIL_CHURN_TEMPLATES) + len(CREDIT_RISK_TEMPLATES) + len(FRAUD_TEMPLATES)
        + len(AML_TEMPLATES) + len(COLLECTIONS_TEMPLATES) + len(DEPOSITS_TEMPLATES)
        + len(PAYMENTS_TEMPLATES))
    # the three new families do not collide ids with each other or the four existing families
    assert not (_ALL_COLL_IDS & _ALL_DEP_IDS) and not (_ALL_DEP_IDS & _ALL_PAY_IDS)
    assert not (_ALL_COLL_IDS & _ALL_PAY_IDS)
    existing = ({t.id for t in RETAIL_CHURN_TEMPLATES} | {t.id for t in CREDIT_RISK_TEMPLATES}
                | {t.id for t in FRAUD_TEMPLATES} | {t.id for t in AML_TEMPLATES})
    assert not ((_ALL_COLL_IDS | _ALL_DEP_IDS | _ALL_PAY_IDS) & existing)
