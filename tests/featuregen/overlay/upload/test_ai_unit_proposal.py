"""E4a Task 2 — the LLM proposes a ``unit``/``currency`` when the file declares none.

A feature whose measure carries no declared unit is created and flagged ``NEEDS_EXTERNAL_VALIDATION``
with a ``UNIT_CONSISTENT`` requirement — but until now nobody could answer it: the FTR export
declares no unit, ``unit`` is not human-editable, and the LLM was never asked. This stage asks the
LLM, and stores the answer as ``llm/proposed`` FIELD EVIDENCE.

**The safety design is the point of this file.** The gauntlet reads ``unit``/``currency`` from
``graph_node`` ONLY (``feature_assist._column_meta`` — a plain SELECT), and ``_MEASURE_ANNOTATION``
keeps both its ``display_rule`` and its ``operational_rule`` at ``_SOURCE_OR_HUMAN``, so an LLM
proposal can never win resolution and is never projected. An AI unit is therefore *structurally*
incapable of clearing the check — not guarded against it, incapable of it. Three tests pin exactly
that: the evidence lands (a), ``graph_node.unit`` is untouched (b), and ``UNIT_CONSISTENT`` STILL
FIRES for the very operand the AI just answered (c).

The remaining two pin the writer's contract: a source-DECLARED unit is never contested (d), and
re-enrichment supersedes the AI's own prior proposal without touching another producer's row (e).

A NON-GENERIC source name is mandatory: a generic one like ``bank`` is created as a schema-less
TECHNICAL source elsewhere in the suite and the source-kind guard would then HOLD this FTR upload in
a full-suite run.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen._helpers import mint_test_identity
from tests.featuregen.overlay.upload.test_ftr_adapter import _HDR, _row

import featuregen.overlay.upload.enrich as enrich_mod
from featuregen.contracts.envelopes import Command, IdentityEnvelope
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.commands import confirm_fact
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import read_active_field_evidence, record_field_evidence
from featuregen.overlay.upload.canonical import validate_rows
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary, to_glossary_upload
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE
from featuregen.overlay.upload.stage_report import StageRecorder
from featuregen.overlay.upload.suggestions import suggest_features_for_table
from featuregen.overlay.upload.table_fact_governance import (
    list_open_table_fact_proposals_governance,
    load_table_fact_confirmation_context,
    project_verified_table_fact,
)

SOURCE = "e4a_unit_proposal_ftr"
TABLE = "unit_fin_tran"
NOW = datetime(2026, 7, 27, tzinfo=UTC)
_SCHEMA = "DPL_EIB_COMPLIANCE"

# column -> (concept, declared type, business term). The concept is what makes a column a MEASURE:
# the drafter targets exactly the columns whose taxonomy concept carries an aggregation behaviour.
_COLUMNS = {
    "CIF_ID": ("customer_id", "varchar", "Customer Identifier"),
    "ACCT_ID": ("account_id", "varchar", "Account Identifier"),
    "TXN_AMT": ("monetary_flow", "decimal", "Transaction Amount"),
    "BAL_AMT": ("monetary_stock", "decimal", "Account Balance"),
    "AS_OF_DT": ("as_of_date", "date", "As Of Date"),
    "TXN_TS": ("event_timestamp", "timestamp", "Transaction Timestamp"),
    "TXN_CNT": ("count", "integer", "Transaction Count"),
    "CUST_HOLD": ("custody_holding", "decimal", "Custody Holding"),
    "SETL_STAT": ("settlement_status", "varchar", "Settlement Status"),
}
# The measure columns — the AI's target set (a unit is meaningful for these and for no others).
_MEASURES = ("TXN_AMT", "BAL_AMT", "TXN_CNT", "CUST_HOLD")

_UPLOADER = IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                             auth_method="oidc", role_claims=("data_owner",))
_ADMIN = mint_test_identity(subject="user:e4a-unit-admin", role_claims=("platform-admin",))

_SYNTHESIS = {"grain_columns": ["cif_id"], "as_of_column": "as_of_dt", "as_of_basis": "posted_at",
              "table_role": "fact", "primary_entity": None, "event_or_snapshot": "event"}


def _seal() -> None:
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _ftr_csv() -> str:
    return _HDR + "".join(
        _row(source_row=str(n), fqn=f"{_SCHEMA}.{TABLE.upper()}.{col}", term_name=term,
             definition=f'"{term}, recorded on {TABLE}."', data_type=declared)
        for n, (col, (_concept, declared, term)) in enumerate(_COLUMNS.items(), start=1))


def _upload():
    return to_glossary_upload(read_ftr_glossary(_ftr_csv(), source=SOURCE))


def _ref(column: str) -> str:
    """The SCHEMA-preserving evidence identity of one column (never the ``public`` twin)."""
    return normalize_ref(SOURCE, _SCHEMA, TABLE, column)


def _client(upload, units: dict[str, str], currencies: dict[str, str] | None = None) -> FakeLLM:
    """A FakeLLM scripted for every Pass A stage + Pass B. The UNIT task answers ``units[column]``
    (and ``currencies[column]``) for the named columns. ``validate_rows`` normalizes row identity
    before enrichment, so a batch item ref is the NORMALIZED row's content hash."""
    good = validate_rows(upload.rows, SOURCE, profile=FTR_GLOSSARY_PROFILE).good
    concepts = {content_hash(r): _COLUMNS[r.column.upper()][0] for r in good}
    hash_of = {r.column.lower(): content_hash(r) for r in good}
    currencies = currencies or {}
    unit_results = []
    for column, unit in units.items():
        entry = {"ref": hash_of[column.lower()], "unit": unit}
        if currencies.get(column.lower()):
            entry["currency"] = currencies[column.lower()]
        unit_results.append(entry)
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"results": [
            {"ref": h, "concept": c} for h, c in concepts.items()]}),
        "overlay.enrich.definition": FakeResponse(output={"results": []}),
        "overlay.enrich.domain": FakeResponse(output={"results": [
            {"ref": TABLE, "domain": "payments"}]}),
        "overlay.enrich.synonyms": FakeResponse(output={"results": []}),
        "overlay.enrich.unit": FakeResponse(output={"results": unit_results}),
        "table_synth_summary": FakeResponse(output={"results": [
            {"ref": f"{TABLE}#chunk0", "summary": {
                "grain_candidates": ["cif_id"], "temporal_candidates": ["as_of_dt"],
                "entity_signals": [], "event_or_snapshot": "event"}}]}),
        "table_synth": FakeResponse(output={"results": [
            {"ref": TABLE, "synthesis": _SYNTHESIS}]}),
    })


def _ingest(conn, *, units, currencies=None, now=NOW, recorder=None):
    upload = _upload()
    res = ingest_upload(conn, SOURCE, upload.rows, actor=_UPLOADER, now=now,
                        client=_client(upload, units, currencies), glossary=upload,
                        stage_recorder=recorder)
    assert res.status == "ingested", res.status
    return upload


@pytest.fixture
def ai_unit_catalog(overlay_conn, monkeypatch):
    """The real FTR catalog after a real ingest with Pass B ON, where the AI proposed a unit +
    currency for every MEASURE column."""
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    _seal()
    _ingest(overlay_conn,
            units={c.lower(): ("AED" if c != "TXN_CNT" else "transactions") for c in _MEASURES},
            currencies={c.lower(): "AED" for c in ("TXN_AMT", "BAL_AMT", "CUST_HOLD")})
    return SOURCE


def _ai(conn, ref: str, field: str) -> list:
    """The ACTIVE ``llm`` proposals for one field at ``ref`` — producer-scoped, so another
    producer's row at the same ref is never counted."""
    return [e for e in read_active_field_evidence(conn, ref, field) if e.producer == "llm"]


def _node_measure(conn, column: str) -> tuple:
    row = conn.execute(
        "SELECT unit, currency FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, f"public.{TABLE}.{column.lower()}")).fetchone()
    return (row[0], row[1])


# ── (a) the proposal lands as governed llm/proposed evidence ──────────────────────────────────────


def test_ai_unit_is_written_as_llm_proposed_field_evidence(overlay_conn, ai_unit_catalog):
    """A measure column whose file declares NO unit gets one drafted by the LLM, stored as
    ``llm/proposed`` ``unit`` (and ``currency``) evidence at the SCHEMA-preserving ref."""
    ev = _ai(overlay_conn, _ref("txn_amt"), "unit")
    assert len(ev) == 1
    assert ev[0].producer == "llm" and ev[0].strength == "proposed"
    assert ev[0].proposed_value == "AED"
    cur = _ai(overlay_conn, _ref("txn_amt"), "currency")
    assert [e.proposed_value for e in cur] == ["AED"]
    # ...at the schema-preserving ref, never the public twin
    assert _ai(overlay_conn, normalize_ref(SOURCE, None, TABLE, "txn_amt"), "unit") == []


def test_only_measure_columns_are_asked_for_a_unit(overlay_conn, ai_unit_catalog):
    """The target set is the honest one: a unit is meaningful for a MEASURE and for nothing else.
    An identifier, a date and a status string are never given an AI unit."""
    for column in ("cif_id", "acct_id", "as_of_dt", "txn_ts", "setl_stat"):
        assert _ai(overlay_conn, _ref(column), "unit") == [], column
        assert _ai(overlay_conn, _ref(column), "currency") == [], column
    for column in ("txn_amt", "bal_amt", "cust_hold"):
        assert _ai(overlay_conn, _ref(column), "unit"), column


# ── (b) the LLM never reaches graph_node ──────────────────────────────────────────────────────────


def test_the_ai_unit_never_reaches_graph_node_unit(overlay_conn, monkeypatch):
    """THE STRUCTURAL GUARANTEE, asserted before AND after: ``graph_node.unit``/``.currency`` are
    what the gauntlet reads, and an ``llm/proposed`` value cannot get there. ``_MEASURE_ANNOTATION``
    excludes the LLM from both its display and its operational rule, so the resolver never projects
    it — the hole is designed out, not guarded."""
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    _seal()
    _ingest(overlay_conn, units={})                    # the same catalog, with the AI silent
    before = _node_measure(overlay_conn, "txn_amt")
    assert before == (None, None), before      # the FTR export declares neither
    assert _ai(overlay_conn, _ref("txn_amt"), "unit") == []

    _ingest(overlay_conn, units={"txn_amt": "AED"}, currencies={"txn_amt": "AED"},
            now=NOW + timedelta(hours=1))

    assert _ai(overlay_conn, _ref("txn_amt"), "unit"), "the AI proposal was not even written"
    assert _node_measure(overlay_conn, "txn_amt") == before, (
        "an llm/proposed unit reached graph_node — the gauntlet reads that column, so the AI "
        "could now silently clear a safety check")


# ── (c) THE LOAD-BEARING SAFETY TEST ───────────────────────────────────────────────────────────────


def _confirm_ai_table_facts(conn) -> None:
    """Dispose of every open AI table-fact proposal through the REAL governance surface, so the
    candidate features get past the grain/as-of gates and reach the unit question."""
    queued = list_open_table_fact_proposals_governance(conn, SOURCE)
    assert queued, "Pass B proposed nothing — the fixture is not exercising the gauntlet"
    for view in queued:
        if view["table"] != TABLE:
            continue
        ctx = load_table_fact_confirmation_context(conn, view["fact_key"])
        res = confirm_fact(conn, Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
             "target_event_id": ctx["target_event_id"]}, _ADMIN,
            f"confirm-{ctx['target_event_id']}"))
        assert res.accepted, f"{ctx['fact_type']}: {res.denied_reason}"
        assert project_verified_table_fact(
            conn, SOURCE, ctx["ref"], ctx["fact_type"], now=NOW) == "projected"


def _requirements(conn) -> Counter:
    out = suggest_features_for_table(conn, catalog_source=SOURCE, table=TABLE)
    assert out["table_known"]
    return Counter(f"{r['code']}@{r['operand'][-1].rsplit('.', 1)[-1]}"
                   for g in out["groups"] for s in g["suggestions"] for r in s["requirements"])


def test_an_ai_proposed_unit_does_not_clear_the_unit_consistent_safety_check(overlay_conn,
                                                                            ai_unit_catalog):
    """THE LOAD-BEARING SAFETY TEST — an AI GUESS MUST NOT CLEAR A SAFETY CHECK.

    The AI has proposed ``AED`` for ``txn_amt``; the proposal is stored, governed and visible. The
    ``UNIT_CONSISTENT`` requirement on that very operand must STILL FIRE, because the check reads
    ``graph_node.unit`` and only a source-attested or human-confirmed value ever gets there.

    If this test ever goes green because the requirement vanished, an unreviewed model guess is
    silently certifying that two measures are unit-compatible — the exact failure the E4a design
    exists to make impossible."""
    _confirm_ai_table_facts(overlay_conn)
    reqs = _requirements(overlay_conn)

    assert _ai(overlay_conn, _ref("txn_amt"), "unit"), "no AI proposal — nothing is being tested"
    assert reqs["UNIT_CONSISTENT@txn_amt"] > 0, (
        "an llm/proposed unit CLEARED UNIT_CONSISTENT — an AI guess is now certifying unit safety")
    assert reqs["CURRENCY_CONSISTENT@txn_amt"] > 0, (
        "an llm/proposed currency CLEARED CURRENCY_CONSISTENT")


# ── (d) a source-declared unit is never contested ─────────────────────────────────────────────────


def test_a_source_declared_unit_is_never_overwritten_or_contested(overlay_conn):
    """The AI fills a BLANK, it never contests a declared value: a column whose FILE declares a unit
    is not a target — no AI proposal is drafted for it, and the SOURCE evidence is untouched."""
    _seal()
    upload = _upload()
    good = validate_rows(upload.rows, SOURCE, profile=FTR_GLOSSARY_PROFILE).good
    # The file DECLARES a unit for txn_amt (a technical source's `unit` column would carry this).
    declared = [replace(r, unit="USD", currency="USD") if r.column.lower() == "txn_amt" else r
                for r in good]
    build_graph(overlay_conn, SOURCE, declared)
    ref = _ref("txn_amt")
    record_field_evidence(
        overlay_conn, logical_ref=ref, field_name="unit", proposed_value="USD",
        producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
        producer_ref="snap", source_snapshot_id="snap", input_hash="src-unit-hash")
    concepts = {content_hash(r): _COLUMNS[r.column.upper()][0] for r in declared}

    assert enrich_mod._unit_targets(declared, concepts) == {
        content_hash(r) for r in declared
        if r.column.upper() in _MEASURES and r.column.lower() != "txn_amt"}

    n = enrich_mod._write_unit_evidence(
        overlay_conn, source=SOURCE, rows=declared, units={content_hash(declared[0]): "AED"},
        currencies={}, concepts=concepts, glossary=upload, bindings=None,
        source_snapshot_id="snap2")

    assert n == 0
    assert _ai(overlay_conn, ref, "unit") == []           # no AI proposal for a declared unit
    source_rows = [e for e in read_active_field_evidence(overlay_conn, ref, "unit")
                   if e.producer == "source"]
    assert [e.proposed_value for e in source_rows] == ["USD"]   # untouched


# ── (e) re-enrichment supersedes the AI's own row only ────────────────────────────────────────────


def test_reenrichment_supersedes_the_prior_ai_unit_and_spares_other_producers(overlay_conn):
    """Producer-scoped supersession: a re-drafted unit replaces the LLM's own prior ACTIVE row
    (exactly one, the new value) while a HUMAN-confirmed row at the same ref survives untouched."""
    _seal()
    upload = _upload()
    good = validate_rows(upload.rows, SOURCE, profile=FTR_GLOSSARY_PROFILE).good
    build_graph(overlay_conn, SOURCE, good)
    concepts = {content_hash(r): _COLUMNS[r.column.upper()][0] for r in good}
    amt = next(r for r in good if r.column.lower() == "txn_amt")
    ref = _ref("txn_amt")
    record_field_evidence(
        overlay_conn, logical_ref=ref, field_name="unit", proposed_value="fils",
        producer=EvidenceProducer.HUMAN, strength=AssertionStrength.CONFIRMED,
        producer_ref="human", source_snapshot_id="snap", input_hash="human-unit-hash")

    for value in ("AED", "dirhams"):
        enrich_mod._write_unit_evidence(
            overlay_conn, source=SOURCE, rows=good, units={content_hash(amt): value},
            currencies={}, concepts=concepts, glossary=upload, bindings=None,
            source_snapshot_id="snap")

    assert [e.proposed_value for e in _ai(overlay_conn, ref, "unit")] == ["dirhams"]
    human = [e for e in read_active_field_evidence(overlay_conn, ref, "unit")
             if e.producer == "human"]
    assert [e.proposed_value for e in human] == ["fils"]


# ── stage wiring ──────────────────────────────────────────────────────────────────────────────────


def test_no_llm_client_records_skipped_and_ingestion_continues(overlay_conn):
    """No provider configured: the unit stage honestly records ``skipped_no_client`` and the upload
    completes normally (the stage is never invented as a failure)."""
    _seal()
    upload = _upload()
    rec = StageRecorder()
    res = ingest_upload(overlay_conn, SOURCE, upload.rows, actor=_UPLOADER, now=NOW,
                        glossary=upload, stage_recorder=rec)

    assert res.status == "ingested"
    assert next(r for r in rec.reports if r.stage == "enrich_unit").state == "skipped_no_client"
    assert _ai(overlay_conn, _ref("txn_amt"), "unit") == []


def test_the_unit_stage_reports_succeeded_when_every_measure_is_drafted(overlay_conn, monkeypatch):
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    _seal()
    rec = StageRecorder()
    _ingest(overlay_conn,
            units={c.lower(): "AED" for c in _MEASURES},
            currencies={c.lower(): "AED" for c in ("TXN_AMT", "BAL_AMT", "CUST_HOLD")},
            recorder=rec)
    report = next(r for r in rec.reports if r.stage == "enrich_unit")
    assert report.state == "succeeded", (report.state, report.detail)
    assert report.detail["expected"] == len(_MEASURES)
