"""B2 credit_risk family — the §B2 DETERIORATION → DEFAULT funnel authored to Part-F depth.

A credit-shaped mini-catalog is built via ``build_graph`` at FACILITY grain: an exposure
(``ead`` + a drawn ``monetary_stock``), a ``limit``, an ``as_of_date``, arrears (``dpd`` /
``delinquency_bucket``), IFRS9 staging (``impairment_stage`` / ``ecl``), forbearance/SICR flags, a
``collateral_value``, a ``covenant``, an installment ``scheduled_amount`` + a ``monetary_flow``
repayment + an ``event_timestamp``, and external bureau signals (``bureau_score`` / ``bureau_inquiry``
/ ``trade_line``) — PLUS two DANGEROUS columns: the ``default_flag`` (a leakage anchor) and a
``protected_attribute``, to prove the engine never binds them. Grounding is deterministic (no LLM), so
these are exact assertions.

Routing is a first-class assertion: grounding is the ROUTER — the credit family surfaces only where its
distinctive concepts exist, so it grounds NOTHING on the churn catalog, and ``ALL_TEMPLATES`` on a churn
catalog yields exactly the churn lens.
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

SOURCE = "credit"

# (CanonicalRow, concept-name-or-None) — a rich facility-grain credit catalog that grounds the whole
# family. The two DANGEROUS columns (default_flag = leakage anchor, borrower_age_band =
# protected_attribute) are deliberately NOT is_grain / is_as_of, so neither a concept pick nor a
# structural pick can bind them.
_CATALOG = [
    # facility grain + the customer link (bureau recipes ground on customer_id)
    (CanonicalRow(SOURCE, "facilities", "facility_id", "integer", is_grain=True, entity="Facility"),
     "facility_id"),
    (CanonicalRow(SOURCE, "facilities", "customer_id", "integer", entity="Customer"), "customer_id"),
    (CanonicalRow(SOURCE, "facilities", "as_of_dt", "timestamp", as_of=True), "as_of_date"),
    # exposure + limit + drawn balance + collateral
    (CanonicalRow(SOURCE, "facilities", "ead", "numeric", additivity="semi_additive", currency="USD"),
     "ead"),
    (CanonicalRow(SOURCE, "facilities", "credit_limit", "numeric", currency="USD"), "limit"),
    (CanonicalRow(SOURCE, "facilities", "drawn_balance", "numeric", additivity="semi_additive",
                  currency="USD"), "monetary_stock"),
    (CanonicalRow(SOURCE, "facilities", "collateral_value", "numeric", additivity="semi_additive",
                  currency="USD"), "collateral_value"),
    # arrears / staging / provisioning (several NEAR-LABEL)
    (CanonicalRow(SOURCE, "facilities", "dpd", "integer"), "dpd"),
    (CanonicalRow(SOURCE, "facilities", "delinquency_bucket", "text"), "delinquency_bucket"),
    (CanonicalRow(SOURCE, "facilities", "impairment_stage", "integer"), "impairment_stage"),
    (CanonicalRow(SOURCE, "facilities", "ecl", "numeric", additivity="semi_additive", currency="USD"),
     "ecl"),
    (CanonicalRow(SOURCE, "facilities", "restructured", "boolean"), "restructured_flag"),
    (CanonicalRow(SOURCE, "facilities", "sicr", "boolean"), "sicr_flag"),
    (CanonicalRow(SOURCE, "facilities", "covenant_headroom", "numeric"), "covenant"),
    # DANGEROUS — never a feature input
    (CanonicalRow(SOURCE, "facilities", "default_flag", "boolean"), "default_flag"),       # leakage anchor
    (CanonicalRow(SOURCE, "facilities", "borrower_age_band", "text"), "protected_attribute"),  # protected
    # repayment schedule + flow + event
    (CanonicalRow(SOURCE, "payments", "scheduled_amount", "numeric", additivity="additive",
                  currency="USD"), "scheduled_amount"),
    (CanonicalRow(SOURCE, "payments", "amount", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(SOURCE, "payments", "payment_ts", "timestamp"), "event_timestamp"),
    # external bureau signals
    (CanonicalRow(SOURCE, "bureau", "bureau_score", "numeric"), "bureau_score"),
    (CanonicalRow(SOURCE, "bureau", "hard_inquiry", "integer"), "bureau_inquiry"),
    (CanonicalRow(SOURCE, "bureau", "tradeline", "text"), "trade_line"),
]

_DANGEROUS_REFS = {"public.facilities.default_flag", "public.facilities.borrower_age_band"}

# The whole family grounds on this rich catalog (every distinctive concept is present).
_ALL_CREDIT_IDS = {t.id for t in CREDIT_RISK_TEMPLATES}


def _credit_catalog(db):
    rows = [r for r, _ in _CATALOG]
    concepts = {content_hash(r): c for r, c in _CATALOG if c}
    build_graph(db, SOURCE, rows, concepts=concepts)


def _by_id(templates):
    return {t.id: t for t in templates}


CREDIT = _by_id(CREDIT_RISK_TEMPLATES)


# ── the family was authored (16 recipes across the funnel) ─────────────────────────────────────────
def test_credit_family_authored_across_the_funnel():
    assert len(CREDIT_RISK_TEMPLATES) == 16
    assert set(CREDIT) == {
        "credit_utilisation", "exposure_trend", "days_past_due_max", "delinquency_bucket_dynamics",
        "payment_ratio", "min_payment_only_streak", "missed_partial_payment_count",
        "ecl_provision_trend", "stage_migration", "loan_to_value", "bureau_score_delta",
        "bureau_inquiry_velocity", "new_trade_line_count", "forbearance_in_window", "sicr_onset",
        "dscr_covenant_headroom"}
    # every credit need references a real concept (also enforced at import by _validate_registry)
    for t in CREDIT_RISK_TEMPLATES:
        for need in t.needs:
            assert need.concept in CONCEPT_REGISTRY


# ── every credit recipe Needs a distinctive concept (the routing discipline) ───────────────────────
def test_every_credit_recipe_anchors_on_a_distinctive_concept():
    # the churn catalog's concept vocabulary — a credit recipe must require at least one concept OUTSIDE
    # it, else grounding would cross-surface it onto a churn-shaped catalog.
    churn_concepts = {c for _r, c in _CHURN_CATALOG if c}
    for t in CREDIT_RISK_TEMPLATES:
        required = {n.concept for n in t.needs if not n.optional}
        assert required - churn_concepts, f"{t.id} has no distinctive required need (would cross-surface)"


# ── the family grounds on a credit-shaped catalog ──────────────────────────────────────────────────
def test_credit_family_grounds_on_a_credit_catalog(db):
    _credit_catalog(db)
    grounded = ground_all(db, CREDIT_RISK_TEMPLATES, catalog_source=SOURCE)
    assert {gf.template_id for gf in grounded} == _ALL_CREDIT_IDS   # the whole funnel realizes here


def test_credit_utilisation_grounds_to_limit_drawn_asof_facility(db):
    _credit_catalog(db)
    gf = ground_template(db, CREDIT["credit_utilisation"], catalog_source=SOURCE)
    assert isinstance(gf, GroundedFeature)
    assert gf.name == "credit_utilisation_90d"            # id + default window (first allowed = 90)
    assert gf.aggregation == "utilisation_90d"
    assert gf.grain_table == "facilities"                 # bound on the facility grain
    assert gf.as_of_column == "as_of_dt"
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.facilities.credit_limit" in refs       # the limit (distinctive anchor)
    assert "public.facilities.drawn_balance" in refs      # the drawn monetary_stock
    assert "public.facilities.facility_id" in refs        # the entity/grain
    assert gf.additivity == "non_additive"                # a utilisation ratio
    assert "point-in-time credit STATE" in gf.pit


# ── safety by construction — the engine NEVER binds the target or a protected column ───────────────
def test_full_credit_set_never_binds_the_target_or_protected_columns(db):
    _credit_catalog(db)
    grounded = ground_all(db, CREDIT_RISK_TEMPLATES, catalog_source=SOURCE)
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert "public.facilities.default_flag" not in all_refs        # the leakage anchor is never bound
    assert "public.facilities.borrower_age_band" not in all_refs   # the protected attribute is never bound
    assert not (all_refs & _DANGEROUS_REFS)


# ── near-label recipes carry near_label True (the 3-part leakage control must flag them) ────────────
def test_near_label_credit_recipes_are_flagged(db):
    _credit_catalog(db)
    expected_near_label = {
        "days_past_due_max", "delinquency_bucket_dynamics", "stage_migration",
        "forbearance_in_window", "sicr_onset", "dscr_covenant_headroom"}
    assert {t.id for t in CREDIT_RISK_TEMPLATES if t.near_label} == expected_near_label
    for tid in expected_near_label:
        gf = ground_template(db, CREDIT[tid], catalog_source=SOURCE)
        assert gf is not None and gf.near_label is True
        assert "NEAR-LABEL" in gf.eligibility              # the ⚠ pre-default note travels onto the candidate


def test_non_near_label_credit_recipes_are_not_flagged():
    for tid in ("credit_utilisation", "exposure_trend", "payment_ratio", "loan_to_value",
                "bureau_score_delta", "ecl_provision_trend"):
        assert CREDIT[tid].near_label is False


# ── a required distinctive need absent -> the recipe SKIPS (degrade path) ──────────────────────────
def test_recipe_skips_when_its_distinctive_concept_is_absent(db):
    # a catalog with a facility grain + as_of but NO dpd/covenant/etc. -> those recipes ungroundable.
    rows = [
        CanonicalRow("mini", "f", "facility_id", "integer", is_grain=True, entity="Facility"),
        CanonicalRow("mini", "f", "as_of_dt", "timestamp", as_of=True),
        CanonicalRow("mini", "f", "ead", "numeric", additivity="semi_additive", currency="USD"),
    ]
    concepts = {content_hash(rows[0]): "facility_id", content_hash(rows[1]): "as_of_date",
                content_hash(rows[2]): "ead"}
    build_graph(db, "mini", rows, concepts=concepts)
    grounded = {gf.template_id for gf in ground_all(db, CREDIT_RISK_TEMPLATES, catalog_source="mini")}
    assert grounded == {"exposure_trend"}                 # only the ead-anchored recipe grounds
    assert "days_past_due_max" not in grounded            # no dpd column -> skip
    assert "dscr_covenant_headroom" not in grounded       # no covenant column -> skip


# ── ROUTING: the credit family does NOT cross-surface on a churn catalog ────────────────────────────
def test_credit_family_does_not_ground_on_a_churn_catalog(db):
    _churn_catalog(db)                                    # the pilot churn catalog — no credit concepts
    grounded = ground_all(db, CREDIT_RISK_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))
    assert grounded == []                                 # grounding routed the whole family away


def test_all_templates_on_a_churn_catalog_yields_only_the_churn_lens(db):
    _churn_catalog(db)
    churn_only = {gf.template_id for gf in
                  ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    combined = {gf.template_id for gf in
                ground_all(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    assert combined == churn_only                         # ALL_TEMPLATES adds no credit ids on churn
    assert not (combined & _ALL_CREDIT_IDS)


# ── registry: ALL_TEMPLATES is the union, globally id-unique, every credit need is a real concept ──
def test_all_templates_registry_is_globally_unique():
    ids = [t.id for t in ALL_TEMPLATES]
    assert len(ids) == len(set(ids))                      # no duplicate id across families
    # ALL_TEMPLATES is the union of every authored family (churn + credit + fraud + AML + collections +
    # deposits + payments — the full seven-family union). The credit family stays globally id-unique
    # within it; extending the registry must not collide an id.
    assert set(ids) == (
        {t.id for t in RETAIL_CHURN_TEMPLATES} | _ALL_CREDIT_IDS
        | {t.id for t in FRAUD_TEMPLATES} | {t.id for t in AML_TEMPLATES}
        | {t.id for t in COLLECTIONS_TEMPLATES} | {t.id for t in DEPOSITS_TEMPLATES}
        | {t.id for t in PAYMENTS_TEMPLATES})
    assert len(ALL_TEMPLATES) == (
        len(RETAIL_CHURN_TEMPLATES) + len(CREDIT_RISK_TEMPLATES) + len(FRAUD_TEMPLATES)
        + len(AML_TEMPLATES) + len(COLLECTIONS_TEMPLATES) + len(DEPOSITS_TEMPLATES)
        + len(PAYMENTS_TEMPLATES))
    # the credit family collides no id with any other family in the full union.
    assert not (_ALL_CREDIT_IDS & (
        {t.id for t in FRAUD_TEMPLATES} | {t.id for t in AML_TEMPLATES}
        | {t.id for t in COLLECTIONS_TEMPLATES} | {t.id for t in DEPOSITS_TEMPLATES}
        | {t.id for t in PAYMENTS_TEMPLATES}))
