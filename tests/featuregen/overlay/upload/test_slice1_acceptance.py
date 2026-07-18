"""Phase-2 Slice 1, Task 6 — END-TO-END acceptance on the committed SYNTHETIC FTR sample.

This is the slice's INTEGRATION GATE: it drives the whole upload path (real FTR reader ->
validate -> Pass A -> the Task-3/4 ColumnMetadataView -> the Pass B two-phase wide-table path)
over the committed synthetic fixture and proves, by inspecting the CAPTURED Pass B request
inputs AND the persisted ``llm_call`` audit rows:

- every captured Pass B column profile carries the TWO type fields — ``operational_type`` stays
  honestly ``unknown`` (a glossary is not the type authority) while ``declared_type`` is the
  non-empty glossary-declared SQL type — never the old conflated ``type`` key;
- the table-level term's ``table_definition`` rides the phase-2 synthesis item's metadata;
- a parser facet the reconciler WITHHELD (identifier-shaped sample vs a timestamp / double
  declared type) never reaches the captured profile, while a non-contradictory control column
  keeps its facet (withholding is an active decision, not a missing producer);
- no planted sample token reaches any captured Pass B request or any persisted
  ``llm_call.redacted_input``;
- the field-aware egress boundary's ``sample_strip`` audit is PERSISTED in
  ``llm_call.input_redaction`` for the definition paths.

[F14](a): FTR definitions are already sanitized at read (``ftr_adapter`` runs
``sanitize_definition`` per row), so the egress re-sanitization finds nothing — the persisted
sample-strip entries exist with ``state == "none"`` here; the non-``none`` strip coverage lives
in the raw-item unit tests (``test_enrich_llm.test_llm_call_records_sample_strip_audit``).
[F14](b): the fixture has no ``:``-containing physical column — the ``:``-roundtrip is covered
by ``test_passb_roster.test_colon_containing_column_round_trips_through_roster_entry_intact``.

Hermetic: scripted request-capturing FakeLLM, no network; the real bank CSV is never used.
"""
from __future__ import annotations

import json

from featuregen.overlay.upload.canonical import UNKNOWN_TYPE

# The sample VALUES planted inside the fixture's sample clauses (same set the Phase-1 gate uses).
_PLANTED_TOKENS = ("1000000000001", "1000000000002", "1000000000003", "3000.75")

_TABLE = "comp_fin_tran"                        # validate_rows lowercases identifiers
_PASS_B_TASKS = ("table_synth_summary", "table_synth")
_AUDIT_ENTRY_KEYS = {"path", "sanitizer_version", "state", "removed_count"}


def _captured_profiles(client) -> dict[str, dict]:
    """Every per-column profile Pass B actually egressed (phase-1 chunk summaries), by column."""
    profiled: dict[str, dict] = {}
    for req in client.requests_for("table_synth_summary"):
        for item in req.inputs["catalog_metadata"]["items"]:
            for prof in item["column_profiles"]:
                profiled[prof["column"]] = prof
    return profiled


def test_slice1_view_flows_into_pass_b_and_egress_audit_persists(db, synthetic_ftr_upload):
    source = "ftr_slice1"
    r = synthetic_ftr_upload(db, source=source)
    assert r.status == "ingested"
    client = synthetic_ftr_upload.client

    # ── 1. EVERY captured Pass B profile keeps the two type fields separate ──
    profiled = _captured_profiles(client)
    assert len(profiled) == 126                 # the complete view reached Pass B
    for column, prof in profiled.items():
        assert prof["operational_type"] == UNKNOWN_TYPE, column
        assert prof["declared_type"], column    # non-empty glossary-declared SQL type
        assert "type" not in prof, column       # the conflated v1 key is never emitted

    # ── 2. the table item's metadata carries the (fenced) table_definition ──
    synth_reqs = client.requests_for("table_synth")
    assert synth_reqs, "Pass B phase-2 synthesis never ran"
    items = [it for req in synth_reqs for it in req.inputs["catalog_metadata"]["items"]]
    table_item = next(it for it in items if it["table"] == _TABLE)
    assert "one row per posted transaction" in table_item["table_definition"]
    # the structured roster in the same item is dual-typed for every column too
    assert len(table_item["column_roster"]) == 126
    for entry in table_item["column_roster"]:
        assert entry.keys() == {"column", "operational_type", "declared_type"}
        assert entry["operational_type"] == UNKNOWN_TYPE and entry["declared_type"]

    # ── 3. reconciled-away parser facets are WITHHELD from the captured profiles ──
    # event_ts (declared timestamp) and settlement_dbl (declared double) carry the fixture's
    # identifier-shaped sample clause — reconcile_profile withholds the contradicted facet, so
    # the captured profile has NO semantic_type key at all.
    for column in ("event_ts", "settlement_dbl"):
        assert "semantic_type" not in profiled[column], column
    # control: the non-contradictory identifier column KEEPS its facet — the machinery is live,
    # so the absence above is an active withholding decision.
    assert profiled["cust_acct_no"]["semantic_type"] == "identifier"

    # ── 4a. no planted sample token in any captured Pass B request ──
    passb_reqs = [req for task in _PASS_B_TASKS for req in client.requests_for(task)]
    assert passb_reqs
    for req in passb_reqs:
        blob = json.dumps(req.inputs)
        for token in _PLANTED_TOKENS:
            assert token not in blob, (req.task, token)

    # ── 4b + 5. the PERSISTED llm_call rows: clean redacted_input + the sample_strip audit ──
    rows = db.execute(
        "SELECT task, redacted_input, input_redaction FROM llm_call "
        "WHERE run_id = 'overlay-enrichment'").fetchall()
    assert {row[0] for row in rows} >= set(_PASS_B_TASKS)   # both Pass B phases were audited
    for task, redacted_input, _ in rows:
        blob = json.dumps(redacted_input)
        for token in _PLANTED_TOKENS:
            assert token not in blob, (task, token)         # never persisted, any task's egress

    for task, definition_path in (("table_synth_summary", "column_profiles.business_definition"),
                                  ("table_synth", "table_definition")):
        strips = [a for _t, _ri, ir in rows if _t == task
                  for a in (ir or {}).get("sample_strip", [])]
        assert strips, f"no persisted sample_strip audit for {task}"
        assert all(_AUDIT_ENTRY_KEYS <= a.keys() for a in strips)
        by_path = [a for a in strips if a["path"] == definition_path]
        assert by_path, f"no {definition_path} sample_strip entry for {task}"
        # [F14](a): FTR definitions were sanitized AT READ, so the egress re-strip verifiably
        # found nothing more to remove — the audit entry exists with state "none".
        assert all(a["state"] == "none" and a["removed_count"] == 0 for a in by_path)
