"""B2 — parametric template engine + the 12 retail_churn recipes.

A small in-DB catalog is built via ``build_graph`` with concept-tagged columns (a monetary_stock
balance, an as_of_date, a customer grain, a monetary_flow amount, an event_timestamp) plus two
DANGEROUS columns — the churn target (an outcome_label leakage anchor) and a protected_attribute — to
prove the engine never binds them. Grounding is deterministic (no LLM), so these are exact assertions.
"""
import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.templates import (
    RETAIL_CHURN_TEMPLATES,
    GroundedFeature,
    Need,
    Template,
    ground_all,
    ground_template,
)

SOURCE = "churn"

# (CanonicalRow, concept-name-or-None) — the concept is applied via build_graph's concepts dict.
_CATALOG = [
    (CanonicalRow(SOURCE, "customers", "customer_id", "integer", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(SOURCE, "customers", "signup_date", "date"), "effective_date"),
    (CanonicalRow(SOURCE, "customers", "churned", "boolean"), "outcome_label"),        # TARGET (leakage)
    (CanonicalRow(SOURCE, "customers", "age_band", "text"), "protected_attribute"),    # protected attr
    (CanonicalRow(SOURCE, "customers", "full_name", "text", sensitivity="pii"), "pii"),
    (CanonicalRow(SOURCE, "accounts", "balance", "numeric", additivity="semi_additive", currency="USD"),
     "monetary_stock"),
    (CanonicalRow(SOURCE, "accounts", "snapshot_date", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive", currency="USD"),
     "monetary_flow"),
    (CanonicalRow(SOURCE, "transactions", "txn_ts", "timestamp"), "event_timestamp"),
    (CanonicalRow(SOURCE, "transactions", "beneficiary_name", "text", sensitivity="pii"), "pii"),
    (CanonicalRow(SOURCE, "transactions", "beneficiary_bank", "text"), "counterparty_id"),
]
# Deliberately ABSENT: transaction_type (dr/cr + DD), category_code (salary tag), product_type
# (holdings) — so their templates degrade / skip.


def _churn_catalog(db):
    rows = [r for r, _ in _CATALOG]
    concepts = {content_hash(r): c for r, c in _CATALOG if c}
    build_graph(db, SOURCE, rows, concepts=concepts)


def _by_id(templates):
    return {t.id: t for t in templates}


TEMPLATES = _by_id(RETAIL_CHURN_TEMPLATES)


def _template(tid):
    return TEMPLATES[tid]


# ── the library authored all 12 ──────────────────────────────────────────────────────────────────
def test_library_has_the_twelve_part_f_templates():
    assert len(RETAIL_CHURN_TEMPLATES) == 12
    assert set(TEMPLATES) == {
        "balance_trend", "dormancy_days", "txn_frequency_trend", "inflow_outflow_ratio",
        "days_below_threshold", "salary_signal", "product_breadth", "tenure_days",
        "balance_volatility", "rfm_composite", "dd_cancellation_rate", "external_own_transfer_trend"}
    # every need references a real concept (also enforced at import by _validate_registry)
    from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY
    for t in RETAIL_CHURN_TEMPLATES:
        for need in t.needs:
            assert need.concept in CONCEPT_REGISTRY


# ── grounding the headline template ──────────────────────────────────────────────────────────────
def test_balance_trend_grounds_to_balance_asof_customer(db):
    _churn_catalog(db)
    gf = ground_template(db, _template("balance_trend"), catalog_source=SOURCE)
    assert isinstance(gf, GroundedFeature)
    assert gf.name == "balance_trend_90d"                # id + default window (first allowed = 90)
    assert gf.aggregation == "trend_90d"
    assert gf.grain_table == "customers"                 # bound on the entity/grain column's table
    assert gf.as_of_column == "snapshot_date"            # the is_as_of column
    refs = {ref for src, ref in gf.derives_pairs}        # derives_pairs = (catalog_source, object_ref)
    assert "public.accounts.balance" in refs            # the monetary_stock
    assert "public.accounts.snapshot_date" in refs      # the as_of_date
    assert "public.customers.customer_id" in refs       # the entity
    assert all(src == SOURCE for src, _ref in gf.derives_pairs)
    assert gf.additivity == "n/a" and gf.explain == "H"
    assert "trailing window" in gf.pit                   # PIT baked in (design-time declaration)


# ── param defaults + rejection ───────────────────────────────────────────────────────────────────
def test_param_default_and_overrides(db):
    _churn_catalog(db)
    t = _template("balance_trend")
    assert ground_template(db, t, catalog_source=SOURCE).params["window"] == 90     # first allowed
    gf60 = ground_template(db, t, catalog_source=SOURCE, params={"window": 60})
    assert gf60.name == "balance_trend_60d" and gf60.params["window"] == 60
    with pytest.raises(ValueError):                      # value not in the allowed tuple
        ground_template(db, t, catalog_source=SOURCE, params={"window": 45})
    with pytest.raises(ValueError):                      # unknown param key
        ground_template(db, t, catalog_source=SOURCE, params={"lookback": 90})


# ── degrade / skip when a REQUIRED need is absent ────────────────────────────────────────────────
def test_dd_cancellation_degrades_when_no_dd_column(db):
    _churn_catalog(db)                                   # no transaction_type column exists
    assert ground_template(db, _template("dd_cancellation_rate"), catalog_source=SOURCE) is None


def test_product_breadth_degrades_when_no_holdings(db):
    _churn_catalog(db)                                   # no product_type column exists
    assert ground_template(db, _template("product_breadth"), catalog_source=SOURCE) is None


# ── optional need absent -> still grounds, degrade recorded ──────────────────────────────────────
def test_optional_needs_degrade_but_still_ground(db):
    _churn_catalog(db)                                   # no transaction_type (direction), no category_code
    io = ground_template(db, _template("inflow_outflow_ratio"), catalog_source=SOURCE)
    assert io is not None                                # required flow/event/entity ground; direction opt.
    assert any("direction" in n for n in io.notes)      # the unmet optional is surfaced, not silent
    sal = ground_template(db, _template("salary_signal"), catalog_source=SOURCE)
    assert sal is not None
    assert any("salary_tag" in n for n in sal.notes)


# ── safety by construction — leakage anchor ──────────────────────────────────────────────────────
def test_engine_refuses_to_bind_a_leakage_anchor(db):
    _churn_catalog(db)                                   # 'churned' is tagged outcome_label (leakage)
    # a (mis-authored) template that NEEDS the outcome label must never bind it -> ungroundable.
    probe = Template(
        id="leak_probe", family="probe", intent="probe",
        needs=(Need("target", "outcome_label"),),
        params={}, aggregation="probe", additivity="n/a", explain="H",
        use_cases=("retail_churn",), pit="")
    assert ground_template(db, probe, catalog_source=SOURCE) is None


# ── safety by construction — protected attribute ─────────────────────────────────────────────────
def test_engine_refuses_to_bind_a_protected_attribute(db):
    _churn_catalog(db)                                   # 'age_band' is tagged protected_attribute
    probe = Template(
        id="prot_probe", family="probe", intent="probe",
        needs=(Need("attr", "protected_attribute"),),
        params={}, aggregation="probe", additivity="n/a", explain="H",
        use_cases=("retail_churn",), pit="")
    assert ground_template(db, probe, catalog_source=SOURCE) is None


def test_structural_grain_pick_is_also_safety_filtered(db):
    # Defense-in-depth: even a STRUCTURAL pick (is_grain) is refused if the column is dangerous. Here the
    # only is_grain column is a protected_attribute, so an entity need cannot ground onto it.
    rows = [
        CanonicalRow("mini", "t", "age_at_grain", "integer", is_grain=True),   # tempting grain pick...
        CanonicalRow("mini", "t", "amount", "numeric"),
    ]
    concepts = {content_hash(rows[0]): "protected_attribute", content_hash(rows[1]): "monetary_flow"}
    build_graph(db, "mini", rows, concepts=concepts)
    probe = Template(
        id="entity_probe", family="probe", intent="probe",
        needs=(Need("entity", "customer_id"),),
        params={}, aggregation="probe", additivity="n/a", explain="H",
        use_cases=("retail_churn",), pit="")
    assert ground_template(db, probe, catalog_source="mini") is None    # refused, not bound


def test_full_set_never_binds_the_target_or_protected_columns(db):
    _churn_catalog(db)
    # ground everything groundable (with a pii role so #12 is included) and check NONE reads the danger.
    grounded = ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=SOURCE, roles=("pii_reader",))
    assert grounded                                      # some templates ground
    all_refs = {ref for gf in grounded for _src, ref in gf.derives_pairs}
    assert "public.customers.churned" not in all_refs   # the outcome label is never a feature input
    assert "public.customers.age_band" not in all_refs  # the protected attribute is never bound


# ── near-label marking on dormancy_days ──────────────────────────────────────────────────────────
def test_dormancy_days_is_marked_near_label(db):
    _churn_catalog(db)
    assert _template("dormancy_days").near_label is True
    gf = ground_template(db, _template("dormancy_days"), catalog_source=SOURCE)
    assert gf is not None and gf.name == "dormancy_days" and gf.near_label is True


# ── PII read-scope on external_own_transfer_trend ────────────────────────────────────────────────
def test_external_own_transfer_needs_pii_role(db):
    _churn_catalog(db)
    t = _template("external_own_transfer_trend")
    # Without the pii role the customer/beneficiary name columns are invisible -> required need unmet.
    assert ground_template(db, t, catalog_source=SOURCE, roles=()) is None
    # With the pii role it grounds, and carries its PII eligibility + declared name-match derivation.
    gf = ground_template(db, t, catalog_source=SOURCE, roles=("pii_reader",))
    assert gf is not None and gf.name == "external_own_transfer_trend_90d"
    assert "PII" in gf.eligibility
    assert any("is_own_external_transfer" in n for n in gf.notes)   # the derived intermediate is declared


# ── ground_all skips the ungroundable ────────────────────────────────────────────────────────────
def test_ground_all_skips_ungroundable(db):
    _churn_catalog(db)
    grounded = {gf.template_id for gf in ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=SOURCE)}
    # groundable with no pii role: stock/flow/event/entity templates all bind their required needs.
    assert grounded == {
        "balance_trend", "dormancy_days", "txn_frequency_trend", "inflow_outflow_ratio",
        "days_below_threshold", "salary_signal", "tenure_days", "balance_volatility", "rfm_composite"}
    # skipped: product_breadth (no product_type), dd_cancellation_rate (no transaction_type),
    # external_own_transfer_trend (pii names hidden without the role).
    assert "product_breadth" not in grounded
    assert "dd_cancellation_rate" not in grounded
    assert "external_own_transfer_trend" not in grounded
    # with the pii role, #12 joins the set.
    with_pii = {gf.template_id for gf in
                ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=SOURCE, roles=("pii_reader",))}
    assert "external_own_transfer_trend" in with_pii


def test_ground_all_use_case_filter(db):
    _churn_catalog(db)
    assert ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=SOURCE, use_case="fraud") == []
    assert ground_all(db, RETAIL_CHURN_TEMPLATES, catalog_source=SOURCE, use_case="retail_churn")
