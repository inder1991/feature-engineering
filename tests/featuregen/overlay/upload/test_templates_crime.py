"""Phase-3 Pass-2 fraud + AML families — the §B3 KILL-CHAIN and §B4 LAUNDERING cycle authored to
Part-F depth.

A crime-shaped mini-catalog is built via ``build_graph`` at customer/card/merchant grain: a
``monetary_flow`` + ``event_timestamp``, the online identifiers ``device_fingerprint`` [pii] and
``geolocation`` [pii], the payment signals ``merchant_id`` / ``mcc`` / ``payment_rail`` / ``corridor`` /
``beneficiary_bank`` / ``debit_credit_indicator`` / ``iso20022_purpose_code``, a ``counterparty_id`` and
``pep_flag`` [pii] — PLUS two DANGEROUS columns: the ``fraud_flag`` (a leakage anchor) and a
``protected_attribute``, to prove the engine never binds them. Grounding is deterministic (no LLM), so
these are exact assertions.

Routing is a first-class assertion (the locked invariant): grounding is the ROUTER — a crime family
surfaces only where its distinctive, NON-STRUCTURAL concepts exist, so it grounds NOTHING on the churn
catalog, and ``ALL_TEMPLATES`` on a churn catalog still yields exactly the churn lens.
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

SOURCE = "crime"

# (CanonicalRow, concept-name-or-None) — a crime-shaped transaction-monitoring catalog that grounds a
# healthy subset of BOTH families. The two DANGEROUS columns (fraud_flag = leakage anchor, age_band =
# protected_attribute) are deliberately NOT is_grain / is_as_of, so neither a concept pick nor a
# structural pick can bind them. Deliberately ABSENT (so those recipes route away / SKIP): scheme,
# country_code, nostro_vostro, swift, on_chain/wallet/stablecoin, sanctions/adverse_media/watchlist,
# alert_id/case_id.
_CATALOG = [
    # customer / card / merchant grain
    (CanonicalRow(SOURCE, "customers", "customer_id", "integer", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(SOURCE, "cards", "card_id", "integer", is_grain=True, entity="CardAccount"), "card_id"),
    (CanonicalRow(SOURCE, "merchants", "merchant_id", "integer", is_grain=True, entity="Merchant"),
     "merchant_id"),
    # transaction flow + event axis
    (CanonicalRow(SOURCE, "txns", "amount", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(SOURCE, "txns", "txn_ts", "timestamp"), "event_timestamp"),
    # online identifiers (PII — need the pii role)
    (CanonicalRow(SOURCE, "txns", "device_fp", "text", sensitivity="pii"), "device_fingerprint"),
    (CanonicalRow(SOURCE, "txns", "geo", "text", sensitivity="pii"), "geolocation"),
    # payment signals (crime-distinctive, non-structural anchors)
    (CanonicalRow(SOURCE, "txns", "rail", "text"), "payment_rail"),
    (CanonicalRow(SOURCE, "txns", "mcc", "text"), "mcc"),
    (CanonicalRow(SOURCE, "txns", "corridor", "text"), "corridor"),
    (CanonicalRow(SOURCE, "txns", "beneficiary_bank", "text"), "beneficiary_bank"),
    (CanonicalRow(SOURCE, "txns", "dr_cr", "text"), "debit_credit_indicator"),
    (CanonicalRow(SOURCE, "txns", "purpose", "text"), "iso20022_purpose_code"),
    (CanonicalRow(SOURCE, "txns", "counterparty_id", "integer", entity="Counterparty"), "counterparty_id"),
    # KYC screening (PII)
    (CanonicalRow(SOURCE, "customers", "pep", "boolean", sensitivity="pii"), "pep_flag"),
    # DANGEROUS — never a feature input
    (CanonicalRow(SOURCE, "txns", "fraud_flag", "boolean"), "fraud_flag"),          # leakage anchor
    (CanonicalRow(SOURCE, "customers", "age_band", "text"), "protected_attribute"),  # protected attr
]

_DANGEROUS_REFS = {"public.txns.fraud_flag", "public.customers.age_band"}

# What grounds on this catalog (with the pii role). Fraud: the whole kill-chain realizes here. AML: the
# recipes anchored on concepts PRESENT here ground; those anchored on absent concepts SKIP.
_ALL_FRAUD_IDS = {t.id for t in FRAUD_TEMPLATES}
_AML_GROUNDS = {
    "structuring_smurfing", "cash_intensity_ratio", "rapid_movement_passthrough", "round_amount_ratio",
    "fan_in_fan_out", "high_risk_corridor_exposure", "dormant_reactivation", "screening_exposure"}
_AML_SKIPS = {"nested_correspondent_flow", "crypto_offramp_exposure", "prior_alert_recidivism"}


def _crime_catalog(db):
    rows = [r for r, _ in _CATALOG]
    concepts = {content_hash(r): c for r, c in _CATALOG if c}
    build_graph(db, SOURCE, rows, concepts=concepts)


def _by_id(templates):
    return {t.id: t for t in templates}


FRAUD = _by_id(FRAUD_TEMPLATES)
AML = _by_id(AML_TEMPLATES)


# ── the families were authored (kill-chain + laundering cycle) ─────────────────────────────────────
def test_crime_families_authored():
    assert len(FRAUD_TEMPLATES) == 11
    assert len(AML_TEMPLATES) == 11
    assert set(FRAUD) == {
        "card_testing_velocity", "device_sharing_velocity", "new_device_flag", "geo_velocity_impossible",
        "first_time_payee_high_value", "merchant_risk_anomaly", "txn_velocity_spike", "amount_zscore_spike",
        "cross_channel_rail_anomaly", "cross_border_burst", "amount_just_under_limit"}
    assert set(AML) == _AML_GROUNDS | _AML_SKIPS
    # every crime need references a real concept (also enforced at import by _validate_registry)
    for t in FRAUD_TEMPLATES + AML_TEMPLATES:
        for need in t.needs:
            assert need.concept in CONCEPT_REGISTRY


# ── ROUTING (the locked invariant): every recipe anchors on a NON-STRUCTURAL distinctive concept ────
def test_every_crime_recipe_anchors_on_a_non_structural_distinctive_concept():
    # The churn catalog's concept vocabulary. A crime recipe must REQUIRE at least one concept that is
    # (a) outside it AND (b) non-structural (not an entity/as_of concept) — an entity/as_of need gets
    # structural is_grain/is_as_of credit in _match, so it would bind a churn grain/as-of column and
    # cross-surface. This is the precise condition the runtime routing test enforces.
    churn_concepts = {c for _r, c in _CHURN_CATALOG if c}
    for t in FRAUD_TEMPLATES + AML_TEMPLATES:
        distinctive = False
        for n in t.needs:
            if n.optional:
                continue
            c = CONCEPT_REGISTRY[n.concept]
            structural = c.entity_link is not None or c.pit_role == "as_of"
            if not structural and n.concept not in churn_concepts:
                distinctive = True
        assert distinctive, f"{t.id} has no non-structural distinctive required need (would cross-surface)"


# ── the families ground a healthy subset on a crime-shaped catalog ──────────────────────────────────
def test_fraud_family_grounds_the_whole_killchain_on_a_crime_catalog(db):
    _crime_catalog(db)
    grounded = ground_all(db, FRAUD_TEMPLATES, catalog_source=SOURCE, roles=("pii_reader",))
    assert {gf.template_id for gf in grounded} == _ALL_FRAUD_IDS   # the whole kill-chain realizes here


def test_aml_family_grounds_a_healthy_subset_on_a_crime_catalog(db):
    _crime_catalog(db)
    grounded = {gf.template_id for gf in
                ground_all(db, AML_TEMPLATES, catalog_source=SOURCE, roles=("pii_reader",))}
    assert grounded == _AML_GROUNDS                      # corridor/dr-cr/purpose/pep-anchored recipes
    assert not (grounded & _AML_SKIPS)                   # nostro/on-chain/watchlist-anchored recipes SKIP


def test_structuring_grounds_to_dr_cr_flow_event_customer(db):
    _crime_catalog(db)
    gf = ground_template(db, AML["structuring_smurfing"], catalog_source=SOURCE)
    assert isinstance(gf, GroundedFeature)
    assert gf.name == "structuring_smurfing_30d"          # id + default window (first allowed = 30)
    assert gf.aggregation == "structuring_count_30d"      # AML windows are trailing DAYS -> _Nd suffix
    assert gf.grain_table == "customers"                  # bound on the customer grain
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.txns.dr_cr" in refs                    # the debit_credit_indicator (distinctive anchor)
    assert "public.txns.amount" in refs                   # the monetary_flow
    assert "public.customers.customer_id" in refs         # the entity/grain
    assert gf.additivity == "additive"                    # a count of sub-threshold deposits
    assert "trailing typology window" in gf.pit


def test_geo_velocity_grounds_to_geolocation_event_customer(db):
    _crime_catalog(db)
    gf = ground_template(db, FRAUD["geo_velocity_impossible"], catalog_source=SOURCE, roles=("pii_reader",))
    assert gf is not None
    assert gf.name == "geo_velocity_impossible"           # fraud uses window_min (no trailing-days suffix)
    refs = {ref for _src, ref in gf.derives_pairs}
    assert "public.txns.geo" in refs                      # the geolocation (pii distinctive anchor)
    assert "real-time" in gf.pit                          # the fraud real-time PIT declaration


# ── safety by construction — the engine NEVER binds the fraud_flag target or a protected column ─────
def test_crime_families_never_bind_the_target_or_protected_columns(db):
    _crime_catalog(db)
    grounded = ground_all(db, FRAUD_TEMPLATES + AML_TEMPLATES, catalog_source=SOURCE, roles=("pii_reader",))
    assert grounded
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert "public.txns.fraud_flag" not in all_refs        # the leakage anchor is never bound
    assert "public.customers.age_band" not in all_refs     # the protected attribute is never bound
    assert not (all_refs & _DANGEROUS_REFS)


# ── near-label recipes carry near_label True (the 3-part leakage control must flag them) ────────────
def test_near_label_crime_recipes_are_flagged(db):
    _crime_catalog(db)
    # Fraud is built from behaviour (velocity / geo / structuring), never the alert outcome -> no fraud
    # recipe is near-label. The near-label tail lives in AML (screening / prior-alert exposure).
    assert {t.id for t in FRAUD_TEMPLATES if t.near_label} == set()
    assert {t.id for t in AML_TEMPLATES if t.near_label} == {"screening_exposure", "prior_alert_recidivism"}
    gf = ground_template(db, AML["screening_exposure"], catalog_source=SOURCE, roles=("pii_reader",))
    assert gf is not None and gf.near_label is True
    assert "NEAR-LABEL" in gf.eligibility                  # the ⚠ pre-alert note travels onto the candidate


# ── a pii-anchored recipe does NOT ground without the pii role ─────────────────────────────────────
def test_pii_anchored_recipe_needs_the_pii_role(db):
    _crime_catalog(db)
    # new_device_flag anchors on device_fingerprint (pii); screening_exposure on pep_flag (pii).
    for tid, fam in (("new_device_flag", FRAUD), ("screening_exposure", AML)):
        assert ground_template(db, fam[tid], catalog_source=SOURCE, roles=()) is None       # pii hidden
        gf = ground_template(db, fam[tid], catalog_source=SOURCE, roles=("pii_reader",))
        assert gf is not None                                                                # pii visible
    # and the whole pii-anchored subset only joins the ground set WITH the role
    without = {gf.template_id for gf in ground_all(db, FRAUD_TEMPLATES + AML_TEMPLATES, catalog_source=SOURCE)}
    with_pii = {gf.template_id for gf in
                ground_all(db, FRAUD_TEMPLATES + AML_TEMPLATES, catalog_source=SOURCE, roles=("pii_reader",))}
    assert {"device_sharing_velocity", "new_device_flag", "geo_velocity_impossible", "screening_exposure"} \
        <= (with_pii - without)


# ── a required distinctive need absent -> the recipe SKIPS (degrade path) ───────────────────────────
def test_recipe_skips_when_its_distinctive_concept_is_absent(db):
    _crime_catalog(db)                                    # no nostro_vostro / on_chain / watchlist columns
    grounded = {gf.template_id for gf in
                ground_all(db, AML_TEMPLATES, catalog_source=SOURCE, roles=("pii_reader",))}
    assert "nested_correspondent_flow" not in grounded    # no nostro/vostro data -> skip
    assert "crypto_offramp_exposure" not in grounded      # no on-chain/wallet data -> skip
    assert "prior_alert_recidivism" not in grounded       # no watchlist/alert history -> skip


# ── ROUTING: the crime families do NOT cross-surface on a churn catalog ─────────────────────────────
def test_crime_families_do_not_ground_on_a_churn_catalog(db):
    _churn_catalog(db)                                    # the pilot churn catalog — no crime concepts
    grounded = ground_all(db, FRAUD_TEMPLATES + AML_TEMPLATES,
                          catalog_source=CHURN_SOURCE, roles=("pii_reader",))
    assert grounded == []                                 # grounding routed BOTH families away


def test_all_templates_on_a_churn_catalog_still_yields_only_the_churn_lens(db):
    _churn_catalog(db)
    churn_only = {gf.template_id for gf in
                  ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    combined = {gf.template_id for gf in
                ground_all(db, ALL_TEMPLATES, catalog_source=CHURN_SOURCE, roles=("pii_reader",))}
    assert combined == churn_only                         # ALL_TEMPLATES adds no crime ids on churn
    assert not (combined & (_ALL_FRAUD_IDS | set(AML)))


# ── registry: ALL_TEMPLATES is the union of all seven families, globally id-unique ─────────────────
def test_all_templates_registry_is_globally_unique_with_crime_families():
    ids = [t.id for t in ALL_TEMPLATES]
    assert len(ids) == len(set(ids))                      # no duplicate id across families
    assert set(ids) == (
        {t.id for t in RETAIL_CHURN_TEMPLATES} | {t.id for t in CREDIT_RISK_TEMPLATES}
        | _ALL_FRAUD_IDS | set(AML)
        | {t.id for t in COLLECTIONS_TEMPLATES} | {t.id for t in DEPOSITS_TEMPLATES}
        | {t.id for t in PAYMENTS_TEMPLATES})
    assert len(ALL_TEMPLATES) == (
        len(RETAIL_CHURN_TEMPLATES) + len(CREDIT_RISK_TEMPLATES) + len(FRAUD_TEMPLATES)
        + len(AML_TEMPLATES) + len(COLLECTIONS_TEMPLATES) + len(DEPOSITS_TEMPLATES)
        + len(PAYMENTS_TEMPLATES))
    # the crime families collide no id with the three new core-3 families.
    assert not ((_ALL_FRAUD_IDS | set(AML)) & (
        {t.id for t in COLLECTIONS_TEMPLATES} | {t.id for t in DEPOSITS_TEMPLATES}
        | {t.id for t in PAYMENTS_TEMPLATES}))
