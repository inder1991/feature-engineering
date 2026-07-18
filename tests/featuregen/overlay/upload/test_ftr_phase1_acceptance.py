"""Task 11 — PG-backed Phase-1 acceptance on the committed SYNTHETIC FTR sample.

This is the INTEGRATION GATE for the Phase-1 LLM-enrichment hardening (Tasks 1-10). It drives the
WHOLE upload path — the real FTR reader, validate → facts → graph spine, Pass A batch enrichment,
the two-phase wide-table Pass B — over a committed synthetic fixture that mirrors the real bank
file's structure (126 column terms + 1 table term, exact 17 FTR headers) but contains ONLY invented,
innocuous tokens (NO real PII — the real ``FTR_Column_Mapping*.csv`` is never committed and stays a
manual proof the user runs). Hermetic: a scripted, request-capturing FakeLLM, no network.

It proves, on a fresh source:
- the file routes through the FTR path (``is_ftr_glossary`` is True) and ingests cleanly;
- the truthful additive counts (Tasks 8): 126 columns, 1 table, 0 quarantined, 127 objects, 126
  containment edges, ``facts_asserted == asserted``;
- FTR-declared types are preserved as NON-operational ``declared_type`` on ``graph_node`` while the
  operational ``data_type`` stays ``unknown`` (Task carried from A1);
- parser reconciliation (Task 3) WITHHELD contradictory evidence: a column whose sample shape parses
  as an identifier but whose declared type is ``timestamp`` / ``double`` gets NO parser
  identifier/representation evidence, while a non-contradictory identifier column keeps it;
- the sanitizer (Task carried from A1) left no planted sample token in any stored definition or any
  outbound LLM request;
- Pass A received sanitized business definitions + declared types, bounded to the 600-char cap
  (Task 4), and Pass B received the complete column metadata — proven by inspecting the FakeLLM's
  CAPTURED request inputs;
- an identical re-upload onto the SAME source is deterministic (``changed_objects == 0``).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE
from featuregen.overlay.upload.ftr_adapter import is_ftr_glossary

_CSV_PATH = Path(__file__).parent / "fixtures" / "ftr_sample_synthetic.csv"

# The sample VALUES planted inside the fixture's sample clauses. The sanitizer must strip every one
# before anything persists or egresses — none may appear in a stored definition or an LLM request.
_PLANTED_TOKENS = ("1000000000001", "1000000000002", "1000000000003", "3000.75")


def _col_node(db, source: str, column: str, *cols):
    return db.execute(
        f"SELECT {', '.join(cols)} FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (source, f"public.comp_fin_tran.{column}")).fetchone()


def _parser_evidence(db, source: str, column: str, field: str) -> list:
    """Active PARSER-produced evidence rows for a column's schema-preserving logical_ref/field."""
    ref = f"{source}::dpl_eib_compliance.comp_fin_tran.{column}"
    return [e for e in read_active_field_evidence(db, ref, field) if e.producer == "parser"]


def test_ftr_sample_accepts_cleanly(db, synthetic_ftr_upload):
    # The fixture headers are the exact FTR multiset — the FTR path really is exercised (a mismatch
    # would route to the generic reader and this whole gate would be inert).
    headers = next(csv.reader(io.StringIO(_CSV_PATH.read_text(encoding="utf-8"))))
    assert is_ftr_glossary(headers) is True

    source = "ftr_accept"
    r = synthetic_ftr_upload(db, source=source)

    # ── Clean ingest + truthful additive counts (Task 8) ──
    assert r.status == "ingested"
    assert r.columns == 126
    assert r.tables == 1
    assert r.quarantined == 0
    assert r.objects_stored == 127                 # 126 columns + 1 table node
    assert r.containment_edges == 126              # one `contains` edge per column
    assert r.facts_asserted == r.asserted          # not double-counted

    # The counts track the REAL persisted graph, not just one another.
    assert db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = %s", (source,)
    ).fetchone()[0] == 127
    assert db.execute(
        "SELECT count(*) FROM graph_edge WHERE catalog_source = %s AND kind = 'contains'",
        (source,)).fetchone()[0] == 126

    # ── FTR-declared types preserved on graph_node; operational type stays UNKNOWN (A1 #1) ──
    for column, declared in (("cust_acct_no", "varchar"), ("event_ts", "timestamp"),
                             ("settlement_dbl", "double"), ("txn_amt", "decimal")):
        node = _col_node(db, source, column, "declared_type", "data_type", "schema_name")
        assert node == (declared, UNKNOWN_TYPE, "DPL_EIB_COMPLIANCE"), (column, node)

    # ── Parser reconciliation WITHHELD contradictory evidence (Task 3) ──
    # A CONTROL identifier column (declared varchar) keeps its parser evidence — proof the machinery
    # is live, so the withholding below is an active decision, not an absence of a producer.
    sem = _parser_evidence(db, source, "cust_acct_no", "semantic_type")
    log = _parser_evidence(db, source, "cust_acct_no", "logical_representation")
    assert {e.proposed_value for e in sem} == {"identifier"}
    assert {e.proposed_value for e in log} == {"numeric_string"}
    # A decimal/amount control also flows through unchanged.
    assert {e.proposed_value for e in
            _parser_evidence(db, source, "txn_amt", "semantic_type")} == {"amount"}
    # The contradictory columns (identifier sample vs a temporal / numeric-measure declared type)
    # get NO parser identifier evidence at all — reconcile_profile withheld it.
    for column in ("event_ts", "settlement_dbl"):
        assert _parser_evidence(db, source, column, "semantic_type") == [], column
        assert _parser_evidence(db, source, column, "logical_representation") == [], column

    # ── No planted sample token survived into any stored definition ──
    for object_ref, definition in db.execute(
            "SELECT object_ref, definition FROM graph_node "
            "WHERE catalog_source = %s AND definition IS NOT NULL", (source,)).fetchall():
        for token in _PLANTED_TOKENS:
            assert token not in definition, (object_ref, token)

    # ── Pass A received sanitized defs + DECLARED types (inspect the captured concept requests) ──
    client = synthetic_ftr_upload.client
    concept_reqs = client.requests_for("overlay.enrich.concept")
    assert concept_reqs, "concept enrichment never ran in batch mode"
    concept_items: dict[str, dict] = {}
    for req in concept_reqs:
        for item in req.inputs["catalog_metadata"]["items"]:
            concept_items[item["column"]] = item
    assert len(concept_items) == 126                # every column reached Pass A exactly once
    # The declared SQL type rides the allowlisted `type` key (A1 R5-5), never the useless "unknown".
    # (validate_rows lowercases identifiers, so the per-item `column` is the lowercased form.)
    assert concept_items["event_ts"]["type"] == "timestamp"
    assert concept_items["settlement_dbl"]["type"] == "double"
    assert concept_items["cust_acct_no"]["type"] == "varchar"
    assert all(item["type"] != UNKNOWN_TYPE for item in concept_items.values())
    # Sanitized business definitions egressed (sample clause stripped) and are 600-capped (Task 4):
    # NARRATIVE_MEMO's raw definition is >600 chars, but its egressed business_definition is bounded.
    memo_def = concept_items["narrative_memo"]["business_definition"]
    assert 0 < len(memo_def) <= 600
    assert concept_items["cust_acct_no"].get("business_definition")

    # No planted token in ANY outbound LLM request (Pass A or Pass B).
    for req in client.requests:
        blob = json.dumps(req.inputs)
        for token in _PLANTED_TOKENS:
            assert token not in blob, (req.task, token)

    # ── Pass B received the COMPLETE column metadata (two-phase wide-table path) ──
    summary_reqs = client.requests_for("table_synth_summary")
    assert summary_reqs, "Pass B wide-table summary phase never ran"
    profiled: dict[str, dict] = {}
    for req in summary_reqs:
        for item in req.inputs["catalog_metadata"]["items"]:
            for prof in item["column_profiles"]:
                profiled[prof["column"]] = prof
    assert len(profiled) == 126                     # every column's descriptor reached Pass B
    assert profiled["event_ts"]["type"] == "timestamp"     # declared type carried into Pass B too
    assert all(p["type"] != UNKNOWN_TYPE for p in profiled.values())
    # The phase-2 synthesis got a COMPLETE roster (name:type for every column).
    synth_reqs = client.requests_for("table_synth")
    assert synth_reqs, "Pass B phase-2 synthesis never ran"
    roster = synth_reqs[0].inputs["catalog_metadata"]["items"][0]["column_roster"]
    assert len(roster) == 126

    # Pass B abstained on the one table (the required abstaining synthesis) — no proposed facts.
    assert r.passb_abstained == 1
    assert r.passb_proposed == 0


def test_reupload_is_deterministic(db, synthetic_ftr_upload):
    a = synthetic_ftr_upload(db, source="ftr_reup")
    b = synthetic_ftr_upload(db, source="ftr_reup")
    assert a.status == b.status == "ingested"
    assert (a.columns, a.tables) == (b.columns, b.tables) == (126, 1)
    assert b.quarantined == 0
    assert b.changed_objects == 0        # nothing changed on an identical re-upload
