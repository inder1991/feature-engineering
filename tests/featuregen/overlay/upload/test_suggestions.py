"""P4 v1 Task 1 — per-table feature suggestions with NO hypothesis, NO intent and NO LLM.

The catalog is built through the REAL FTR path (``read_ftr_glossary`` -> ``to_glossary_upload`` ->
``ingest_upload``) under its OWN source name: a generic name like ``bank`` is created as a
schema-less TECHNICAL source elsewhere in the suite, and the source-kind guard would then HOLD this
FTR upload in a full-suite run. The table's grain / availability facts are then GOVERNED through the
real propose -> confirm -> project path, because ``_validate_idea`` refuses a windowed candidate on a
table with no point-in-time basis.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_ftr_adapter import _HDR, _row

from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.commands import confirm_fact, propose_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE
from featuregen.overlay.upload.suggestions import render_recipe, suggest_features_for_table
from featuregen.overlay.upload.table_fact_governance import (
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)
from featuregen.overlay.upload.upload_catalog import table_ref

SOURCE = "p4_suggestions_ftr"
TABLE = "comp_fin_tran"
OTHER_TABLE = "mkt_risk_pos"
NOW = datetime(2026, 7, 27, tzinfo=UTC)
_FQN_PREFIX = "DPL_EIB_COMPLIANCE."

# table -> column -> (concept, declared type, business term). The concepts are what the enrichment
# stage proposes; grounding is the router, so this is what decides which template families surface.
# TWO tables and TWO source entities on purpose: a per-table filter and a per-entity grouping can
# only be shown to work by a fixture that would expose them being wrong.
_COLUMNS = {
    TABLE: {
        "CIF_ID": ("customer_id", "varchar", "Customer Identifier"),
        "ACCT_ID": ("account_id", "varchar", "Account Identifier"),
        "TXN_AMT": ("monetary_flow", "decimal", "Transaction Amount"),
        "BAL_AMT": ("monetary_stock", "decimal", "Account Balance"),
        "AS_OF_DT": ("as_of_date", "date", "As Of Date"),
        "TXN_TS": ("event_timestamp", "timestamp", "Transaction Timestamp"),
        "TXN_CNT": ("count", "integer", "Transaction Count"),
        "CUST_HOLD": ("custody_holding", "decimal", "Custody Holding"),
        "SETL_STAT": ("settlement_status", "varchar", "Settlement Status"),
    },
    OTHER_TABLE: {
        "BOOK_ID": ("book_id", "varchar", "Trading Book Identifier"),
        "VAR_AMT": ("var", "decimal", "Value At Risk"),
        "RISK_DT": ("as_of_date", "date", "Risk As Of Date"),
    },
}
_CONCEPT_OF = {col: spec[0] for cols in _COLUMNS.values() for col, spec in cols.items()}

_SERVICE = IdentityEnvelope(subject="featuregen-overlay-enrichment", actor_kind="service",
                            authenticated=True, auth_method="internal", role_claims=())
_UPLOADER = IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))


class _Catalog(NamedTuple):
    source: str
    table: str


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _ftr_csv() -> str:
    return _HDR + "".join(
        _row(source_row=str(n), fqn=f"{_FQN_PREFIX}{table.upper()}.{col}", term_name=term,
             definition=f'"{term}, recorded on {table}."', data_type=declared)
        for n, (table, col, (_concept, declared, term)) in enumerate(
            ((t, c, spec) for t, cols in _COLUMNS.items() for c, spec in cols.items()), start=1))


def _govern_table_facts(conn, table: str, grain_column: str, as_of_column: str) -> None:
    """Grain + availability through the REAL governance path: the service enrichment actor proposes,
    a platform admin confirms, the confirm-time bridge projects onto ``graph_node``."""
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    ref = table_ref(SOURCE, table)
    for fact_type, value in (("grain", {"columns": [grain_column], "is_unique": True}),
                             ("availability_time", {"column": as_of_column, "basis": "posted_at"})):
        res = propose_fact(conn, Command(
            "propose_fact", "overlay_fact", None,
            {"ref": ref, "fact_type": fact_type, "proposed_value": value},
            _SERVICE, proposal_fingerprint(value)))
        assert res.accepted, res.denied_reason
        ctx = load_table_fact_confirmation_context(conn, fact_key(ref, fact_type))
        res = confirm_fact(conn, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
             "target_event_id": ctx["target_event_id"]},
            admin, f"confirm-{ctx['target_event_id']}"))
        assert res.accepted, res.denied_reason
        assert project_verified_table_fact(conn, SOURCE, ref, fact_type, now=NOW) == "projected"


@pytest.fixture
def ftr_catalog(overlay_conn):
    _seal()
    upload = to_glossary_upload(read_ftr_glossary(_ftr_csv(), source=SOURCE))
    good = validate_rows(upload.rows, SOURCE, profile=FTR_GLOSSARY_PROFILE).good
    concepts = {content_hash(r): _CONCEPT_OF[r.column.upper()] for r in good}
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": c} for h, c in concepts.items()]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": t, "domain": "payments"} for t in _COLUMNS]}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
    })
    res = ingest_upload(overlay_conn, SOURCE, upload.rows, actor=_UPLOADER, now=NOW,
                        client=client, glossary=upload)
    assert res.status == "ingested", res.status
    _govern_table_facts(overlay_conn, TABLE, "cif_id", "as_of_dt")
    _govern_table_facts(overlay_conn, OTHER_TABLE, "book_id", "risk_dt")
    return _Catalog(source=SOURCE, table=TABLE)


def test_suggests_features_for_a_table_without_any_hypothesis(overlay_conn, ftr_catalog):
    """The whole point: no intent, no hypothesis, no LLM — just the catalog."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    assert out["summary"]["suggested"] >= 1
    # counts are the REAL tri-state, not invented
    assert out["summary"]["clean_ready"] + out["summary"]["needs_review"] == out["summary"]["suggested"]
    s = out["groups"][0]["suggestions"][0]
    assert s["description"]          # Template.intent, a real SME sentence
    assert s["validation_status"] in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")
    assert s["uses"]                 # the columns it binds


def _names(out: dict) -> set[str]:
    return {s["name"] for g in out["groups"] for s in g["suggestions"]}


def test_only_this_tables_suggestions_are_returned(overlay_conn, ftr_catalog):
    """The catalog holds TWO governed tables, so dropping the per-table filter is visible: without a
    second table's features in the engine's catalog-wide result there is nothing to leak."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    other = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=OTHER_TABLE)
    assert _names(other)                            # the fixture really does ground on both tables
    assert not (_names(out) & _names(other))
    for g in out["groups"]:
        for s in g["suggestions"]:
            assert s["grain_table"] == ftr_catalog.table


def test_grouped_by_entity(overlay_conn, ftr_catalog):
    """Two DISTINCT source entities on the ONE table: the recipes anchored on ``customer_id`` bind
    ``cif_id``, the custody / settlement recipes anchor on ``account_id`` and bind ``acct_id``. The
    table's single ``is_grain`` column is ``cif_id``, so a grouping taken from the table grain files
    the account-grained features under the customer heading — this is what that would look like."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    by_label = {g["entity_label"]: g for g in out["groups"]}
    assert set(by_label) == {"customer", "account"}
    assert out["summary"]["entities"] == 2
    assert by_label["account"]["entity_ref"] == f"public.{TABLE}.acct_id"
    assert by_label["customer"]["entity_ref"] == f"public.{TABLE}.cif_id"
    # custody_holding_dynamics is bound to acct_id (account), NOT to the table's cif_id grain
    assert "custody_holding_dynamics_90d" in {s["name"] for s in by_label["account"]["suggestions"]}
    assert "balance_trend_90d" in {s["name"] for s in by_label["customer"]["suggestions"]}


def test_rejections_are_this_tables_only(overlay_conn, ftr_catalog):
    """A rejection is a claim ABOUT this table's catalog readiness, but the engine's rejection list is
    catalog-wide and carries no grain — an unfiltered pass-through shows one table's rejects on another."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    other = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=OTHER_TABLE)
    assert out["rejections"]                        # this table really does reject something
    assert not ({r["name"] for r in out["rejections"]} & {r["name"] for r in other["rejections"]})


def _idea(**kw) -> FeatureIdea:
    """A bare FeatureIdea carrying only the typed computation operands the renderer reads."""
    return FeatureIdea(name="f", description="", derives_from=[], aggregation=None,
                       grain_table=TABLE, **kw)


def test_recipe_renders_the_real_operation_label_not_an_invented_sql_verb():
    """``operation_kind`` is a DOMAIN label (~152 values: trend / inflow_outflow / frequency_trend),
    NOT a SQL verb. Printing ``AVG(...)`` for an operation the system calls ``trend`` would be a
    confident-looking invention, so the line carries the label the engine actually bound."""
    line = render_recipe(_idea(
        operation_kind="trend_90d",
        measure_refs=((SOURCE, f"public.{TABLE}.bal_amt"), (SOURCE, f"public.{TABLE}.cif_id"),
                      (SOURCE, f"public.{TABLE}.as_of_dt")),
        grain_ref=(SOURCE, f"public.{TABLE}.cif_id"),
        time_ref=(SOURCE, f"public.{TABLE}.as_of_dt"), window="90d"))
    assert line == "trend_90d(bal_amt) BY cif_id OVER 90d [as_of_dt]"


def test_recipe_does_not_render_the_grain_or_time_column_as_a_measure():
    """``measure_refs`` carries EVERY bound pair, grain and point-in-time included. A card that lists
    the grain column as a measure claims the feature aggregates its own key."""
    idea = _idea(operation_kind="inflow_outflow_30d",
                 measure_refs=((SOURCE, f"public.{TABLE}.cif_id"),
                               (SOURCE, f"public.{TABLE}.txn_amt"),
                               (SOURCE, f"public.{TABLE}.as_of_dt")),
                 grain_ref=(SOURCE, f"public.{TABLE}.cif_id"),
                 time_ref=(SOURCE, f"public.{TABLE}.as_of_dt"), window="30d")
    assert render_recipe(idea) == "inflow_outflow_30d(txn_amt) BY cif_id OVER 30d [as_of_dt]"


def test_recipe_omits_the_clauses_that_do_not_apply():
    """An idea with no window and no point-in-time column must render cleanly — no dangling ``OVER``,
    no empty ``[]``, no trailing space."""
    line = render_recipe(_idea(
        operation_kind="product_breadth",
        measure_refs=((SOURCE, f"public.{TABLE}.setl_stat"), (SOURCE, f"public.{TABLE}.cif_id")),
        grain_ref=(SOURCE, f"public.{TABLE}.cif_id")))
    assert line == "product_breadth(setl_stat) BY cif_id"
    assert "OVER" not in line and "[" not in line
    assert line == line.strip()


def test_recipe_renders_multiple_measures_in_a_stable_order():
    """Two real measures, rendered in the order the engine bound them — same idea, same line."""
    idea = _idea(operation_kind="payment_ratio",
                 measure_refs=((SOURCE, f"public.{TABLE}.txn_amt"),
                               (SOURCE, f"public.{TABLE}.bal_amt"),
                               (SOURCE, f"public.{TABLE}.cif_id")),
                 grain_ref=(SOURCE, f"public.{TABLE}.cif_id"))
    assert render_recipe(idea) == "payment_ratio(txn_amt, bal_amt) BY cif_id"
    assert render_recipe(idea) == render_recipe(idea)


def test_every_card_carries_a_recipe_and_its_parts(overlay_conn, ftr_catalog):
    """The card's recipe line and the structured pieces behind it — on EVERY suggestion, from the
    real grounded engine, not a hand-built idea."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    cards = [s for g in out["groups"] for s in g["suggestions"]]
    assert cards
    for s in cards:
        assert s["recipe"] and s["recipe"] == s["recipe"].strip()
        parts = s["recipe_parts"]
        assert parts["operation"] and parts["measures"]
        assert parts["grain"] not in parts["measures"]      # the grain is never a measure
        assert parts["time"] not in parts["measures"]
        if not parts["window"]:
            assert "OVER" not in s["recipe"]
        if not parts["time"]:
            assert "[" not in s["recipe"]


def test_the_recipe_by_clause_names_the_cards_own_entity(overlay_conn, ftr_catalog):
    """The heading and the line must agree. The card's entity is the recipe's BOUND entity, but the
    ``BY`` clause was taken from the TABLE grain — so an account-grained card read "per account" over
    ``custody_holding_dynamics_90d(cust_hold, acct_id) BY cif_id ...``, naming a key it is not
    computed per AND listing its own key as a measure. The account group is the proof: its entity
    (``acct_id``) is NOT the table's single ``is_grain`` column (``cif_id``)."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    account = next(g for g in out["groups"] if g["entity_label"] == "account")
    assert "custody_holding_dynamics_90d" in {s["name"] for s in account["suggestions"]}
    for group in out["groups"]:
        column = group["entity_ref"].rsplit(".", 1)[-1]
        for s in group["suggestions"]:
            assert s["recipe_parts"]["grain"] == column
            assert f" BY {column}" in s["recipe"]
            assert column not in s["recipe_parts"]["measures"]


def test_writes_nothing(overlay_conn, ftr_catalog):
    """v1 is strictly read-only — the load-bearing guarantee. A row COUNT cannot see an IN-PLACE write
    (``UPDATE graph_node SET is_grain = false`` keeps the cardinality), so fingerprint the row CONTENT."""
    tables = ("field_evidence", "field_decision_event", "graph_node", "contract_intent")
    def fingerprint():
        return tuple(overlay_conn.execute(
            f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            f"FROM {t} t").fetchone() for t in tables)
    before = fingerprint()
    suggest_features_for_table(overlay_conn, catalog_source=ftr_catalog.source,
                               table=ftr_catalog.table)
    assert fingerprint() == before
