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
    """Every per-column profile Pass B actually egressed, by column — from WHICHEVER Pass-B request
    carries them.

    It used to read `table_synth_summary` only, because a 126-column table was WIDE
    (`enrich_llm._MAX_COLUMN_PROFILES` was 64, and that constant is Pass B's narrow/wide router).
    The 2026-08-06 zero-truncation raise took it to 512, so the fixture's table now takes the narrow
    fast path and its profiles ride the single `table_synth` item instead. The property this gate
    exists for — every column's profile egresses with its two type fields separate — is about the
    profiles, not about which call carries them, so the helper reads both tasks.
    """
    profiled: dict[str, dict] = {}
    for task in _PASS_B_TASKS:
        for req in client.requests_for(task):
            for item in req.inputs["catalog_metadata"]["items"]:
                for prof in item.get("column_profiles") or []:
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
    # Every column of the table rides the SAME item as the definition, dual-typed.
    #
    # It used to be the compact `column_roster` (the wide two-phase item's names-and-types digest).
    # Since `_MAX_COLUMN_PROFILES` went 64 -> 512 this table is narrow, so the item carries the FULL
    # `column_profiles` instead — a superset: same identity keys, plus the concept and the business
    # definition the roster had to drop. Read whichever the route produced, and assert the property.
    inventory = table_item.get("column_profiles") or table_item["column_roster"]
    assert len(inventory) == 126
    for entry in inventory:
        # The three identity keys are always present; profile Task 4 additionally carries the
        # resolved `concept` where Pass A produced one (the crosswalk contradiction's only signal).
        assert {"column", "operational_type", "declared_type"} <= entry.keys()
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
    audited_tasks = {row[0] for row in rows}
    # NAMED, not "at least one of": the narrow route is deterministic for this 126-column fixture,
    # so exactly `table_synth` must be audited. `>= set(_PASS_B_TASKS)` became unsatisfiable when
    # the route changed, but weakening it to an intersection would let a regression that drops the
    # `table_synth` audit entirely still pass.
    assert "table_synth" in audited_tasks
    assert "table_synth_summary" not in audited_tasks   # the wide phase must not have run
    for task, redacted_input, _ in rows:
        blob = json.dumps(redacted_input)
        for token in _PLANTED_TOKENS:
            assert token not in blob, (task, token)         # never persisted, any task's egress

    # BOTH definition paths must be audited SOMEWHERE in Pass B. Which phase carries which is a
    # ROUTE detail that moved on 2026-08-06: the column profiles used to ride `table_synth_summary`
    # (the wide two-phase path) and now ride `table_synth` with the table definition, because
    # `_MAX_COLUMN_PROFILES` 64 -> 512 made this 126-column table narrow. The audit coverage — every
    # definition that egressed has a persisted sample_strip entry — is what this pins.
    passb_strips = [a for _t, _ri, ir in rows if _t in _PASS_B_TASKS
                    for a in (ir or {}).get("sample_strip", [])]
    assert passb_strips, "no persisted sample_strip audit for any Pass B phase"
    assert all(_AUDIT_ENTRY_KEYS <= a.keys() for a in passb_strips)
    for definition_path in ("column_profiles.business_definition", "table_definition"):
        by_path = [a for a in passb_strips if a["path"] == definition_path]
        assert by_path, f"no {definition_path} sample_strip entry in any Pass B phase"
        # [F14](a): FTR definitions were sanitized AT READ, so the egress re-strip verifiably
        # found nothing more to remove — the audit entry exists with state "none".
        assert all(a["state"] == "none" and a["removed_count"] == 0 for a in by_path)
