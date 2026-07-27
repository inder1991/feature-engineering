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
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE
from featuregen.overlay.upload.suggestions import suggest_features_for_table
from featuregen.overlay.upload.table_fact_governance import (
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)
from featuregen.overlay.upload.upload_catalog import table_ref

SOURCE = "p4_suggestions_ftr"
TABLE = "comp_fin_tran"
NOW = datetime(2026, 7, 27, tzinfo=UTC)
_FQN_PREFIX = "DPL_EIB_COMPLIANCE.COMP_FIN_TRAN."

# (column -> concept, declared type, business term). The concepts are what the enrichment stage
# proposes; grounding is the router, so this is what decides which template families surface.
_COLUMNS = {
    "CIF_ID": ("customer_id", "varchar", "Customer Identifier"),
    "ACCT_ID": ("account_id", "varchar", "Account Identifier"),
    "TXN_AMT": ("monetary_flow", "decimal", "Transaction Amount"),
    "BAL_AMT": ("monetary_stock", "decimal", "Account Balance"),
    "AS_OF_DT": ("as_of_date", "date", "As Of Date"),
    "TXN_TS": ("event_timestamp", "timestamp", "Transaction Timestamp"),
    "TXN_CNT": ("count", "integer", "Transaction Count"),
}

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
        _row(source_row=str(n), fqn=_FQN_PREFIX + col, term_name=term,
             definition=f'"{term} of the compliance transaction."', data_type=declared)
        for n, (col, (_concept, declared, term)) in enumerate(_COLUMNS.items(), start=1))


def _govern_table_facts(conn) -> None:
    """Grain + availability through the REAL governance path: the service enrichment actor proposes,
    a platform admin confirms, the confirm-time bridge projects onto ``graph_node``."""
    admin = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
    ref = table_ref(SOURCE, TABLE)
    for fact_type, value in (("grain", {"columns": ["cif_id"], "is_unique": True}),
                             ("availability_time", {"column": "as_of_dt", "basis": "posted_at"})):
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
    concepts = {content_hash(r): _COLUMNS[r.column.upper()][0] for r in good}
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": c} for h, c in concepts.items()]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": TABLE, "domain": "payments"}]}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
    })
    res = ingest_upload(overlay_conn, SOURCE, upload.rows, actor=_UPLOADER, now=NOW,
                        client=client, glossary=upload)
    assert res.status == "ingested", res.status
    _govern_table_facts(overlay_conn)
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


def test_only_this_tables_suggestions_are_returned(overlay_conn, ftr_catalog):
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    for g in out["groups"]:
        for s in g["suggestions"]:
            assert s["grain_table"] == ftr_catalog.table


def test_grouped_by_entity(overlay_conn, ftr_catalog):
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    labels = [g["entity_ref"] for g in out["groups"]]
    assert len(labels) == len(set(labels))          # one group per entity, no duplicates
    assert out["summary"]["entities"] == len(labels)


def test_writes_nothing(overlay_conn, ftr_catalog):
    """v1 is strictly read-only — the load-bearing guarantee."""
    def counts():
        return tuple(overlay_conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                     for t in ("field_evidence", "field_decision_event", "graph_node",
                               "contract_intent"))
    before = counts()
    suggest_features_for_table(overlay_conn, catalog_source=ftr_catalog.source,
                               table=ftr_catalog.table)
    assert counts() == before
