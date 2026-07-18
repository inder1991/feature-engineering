"""Task 4 (Phase-2 Slice 1) — Pass B input from the TableMetadataView (schema/prompt v2).

The descriptor carries `operational_type` + `declared_type` as TWO fields (never a conflated
`type`); the wide-table roster entry is a STRUCTURED `{column, operational_type, declared_type}`
object (a column name may itself contain `:`/`/`, which the old `name:type` string conflated
irrecoverably); the item metadata carries the fenced `table_definition`; and both Pass B batch
calls ship the Slice-2 stamp via the Task-1 version seam — prompt v3, canonical schema v2 (the
schema deliberately stays v2: [F1], the role vocab is code-side, never a schema enum). Views are
constructed via the REAL `build_table_views` — no hand-rolled view objects.
"""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import table_synth as ts
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_view import build_table_views
from featuregen.overlay.upload.enrich_llm import (
    _MAX_ROSTER,
    _item_egress_ok,
    _roster_entry_ok,
)
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.table_synth import (
    _roster_entry,
    assemble_table_items,
    synthesize_tables,
)


def _row(t, c, typ="unknown"):
    return CanonicalRow(source="s", table=t, column=c, type=typ, definition="")


def _rec(ref, **kw):
    base = dict(logical_ref=ref, term_name="T", definition="A settled amount.",
                declared_type="double", term_type="measure", domain="Payments")
    base.update(kw)
    return GlossaryRecord(**base)


def _views(rows, records=None):
    g = GlossaryUpload(rows=rows, records=records) if records is not None else None
    return build_table_views(rows, glossary=g, bindings=None,
                             concepts={}, definitions={}, domains={})


class _RecordingLLM(FakeLLM):
    def __init__(self, script):
        super().__init__(script=script)
        self.requests = []

    def call(self, request):
        self.requests.append(request)
        return super().call(request)


# ── dual-type descriptor ────────────────────────────────────────────────────────────────────────


def test_descriptor_dual_types_never_a_conflated_type_key():
    rows = [_row("txn", "fee", typ="unknown")]
    views = _views(rows, records=[_rec("s::banking.txn.fee")])
    items = assemble_table_items(views)
    prof = items[0].metadata["column_profiles"][0]
    assert prof["operational_type"] == "unknown"     # the row's physical type, NOT the declared one
    assert prof["declared_type"] == "double"         # the glossary-DECLARED SQL type (a hint)
    assert "type" not in prof                        # the conflated key is GONE
    assert prof["business_definition"] == "A settled amount."
    assert prof["term_type"] == "measure" and prof["domain"] == "Payments"
    assert _item_egress_ok(items[0].metadata) is True


def test_technical_upload_descriptor_keeps_both_fields_blank_declared():
    rows = [_row("txn", "id", typ="string")]
    items = assemble_table_items(_views(rows))
    prof = items[0].metadata["column_profiles"][0]
    assert prof["operational_type"] == "string" and prof["declared_type"] == ""
    assert "type" not in prof
    assert _item_egress_ok(items[0].metadata) is True


# ── table_definition on the item metadata ───────────────────────────────────────────────────────


def test_table_definition_rides_item_metadata_when_view_has_one():
    rows = [_row("txn", "fee")]
    records = [_rec("s::banking.txn.fee"),
               _rec("s::banking.txn", is_table=True, definition="All card transactions.",
                    term_name="Transactions")]
    items = assemble_table_items(_views(rows, records=records))
    assert items[0].metadata["table_definition"] == "All card transactions."
    assert _item_egress_ok(items[0].metadata) is True


def test_no_table_definition_key_when_view_has_none():
    rows = [_row("txn", "id")]
    items = assemble_table_items(_views(rows))
    assert "table_definition" not in items[0].metadata


# ── structured roster entries ───────────────────────────────────────────────────────────────────


def test_colon_containing_column_round_trips_through_roster_entry_intact():
    rows = [_row("txn", "weird:col/name", typ="string")]
    items = assemble_table_items(_views(rows))
    entry = _roster_entry(items[0].metadata["column_profiles"][0])
    assert entry == {"column": "weird:col/name", "operational_type": "string",
                     "declared_type": ""}                      # NOT "weird:col/name:string"
    assert _roster_entry_ok(entry) is True


def test_roster_entry_validator_rejects_unknown_keys_and_flat_strings():
    ok = {"column": "a:b", "operational_type": "integer", "declared_type": "double"}
    assert _roster_entry_ok(ok) is True
    assert _roster_entry_ok({"column": "c", "rows": "secret"}) is False   # unknown key
    assert _roster_entry_ok({"column": 1}) is False                       # non-string value
    assert _roster_entry_ok("c:integer") is False                         # the OLD flat string
    assert _item_egress_ok({"table": "t", "column_roster": [ok]}) is True
    assert _item_egress_ok({"table": "t", "column_roster": ["c:integer"]}) is False
    assert _item_egress_ok({"table": "t", "column_roster": [ok] * (_MAX_ROSTER + 1)}) is False


# ── wide two-phase path: roster + table_definition threading + v2 seam ──────────────────────────


def test_wide_phase2_item_carries_structured_roster_and_table_definition(db):
    n = 70                                                     # >64 -> 2 chunks
    rows = [_row("ftr", f"c{i}", typ="integer") for i in range(n)]
    records = [_rec("s::banking.ftr", is_table=True, definition="All FTR postings.")]
    items = assemble_table_items(_views(rows, records=records))
    assert items[0].metadata["table_definition"] == "All FTR postings."
    summ = {"grain_candidates": ["c0"], "temporal_candidates": [], "entity_signals": [],
            "event_or_snapshot": None}
    client = _RecordingLLM({
        "table_synth_summary": FakeResponse(output={"results": [
            {"ref": "ftr#chunk0", "summary": summ}, {"ref": "ftr#chunk1", "summary": summ}]}),
        "table_synth": FakeResponse(output={"results": [
            {"ref": "ftr", "synthesis": {"grain_columns": ["c0"]}}]}),
    })
    out = synthesize_tables(db, client, items,
                            columns_by_table={"ftr": {f"c{i}" for i in range(n)}}, actor=None)
    assert out["ftr"]["grain"] == {"columns": ["c0"], "is_unique": True}

    synth_req = [r for r in client.requests if r.task == "table_synth"][0]
    meta = synth_req.inputs["catalog_metadata"]["items"][0]
    assert meta["table_definition"] == "All FTR postings."     # threaded into the PHASE-2 item
    assert len(meta["column_roster"]) == n
    assert meta["column_roster"][0] == {"column": "c0", "operational_type": "integer",
                                        "declared_type": ""}  # structured, not "c0:integer"
    # both phases ship the Slice-2 contract via the Task-1 seam: prompt v3, canonical schema
    # STAYS v2 ([F1] — the role vocab is code-side + prompt-side, never a schema enum)
    assert synth_req.prompt_version == 3 and synth_req.output_schema_version == 2
    summary_req = [r for r in client.requests if r.task == "table_synth_summary"][0]
    assert summary_req.prompt_version == 3 and summary_req.output_schema_version == 2


def test_narrow_fast_path_ships_v3_prompt_v2_schema(db):
    rows = [_row("narrow", "c0")]
    items = assemble_table_items(_views(rows))
    client = _RecordingLLM({"table_synth": FakeResponse(output={"results": [
        {"ref": "narrow", "synthesis": {"grain_columns": ["c0"]}}]})})
    out = synthesize_tables(db, client, items, columns_by_table={"narrow": {"c0"}}, actor=None)
    assert out["narrow"]["grain"] == {"columns": ["c0"], "is_unique": True}
    req = [r for r in client.requests if r.task == "table_synth"][0]
    assert req.prompt_version == 3 and req.output_schema_version == 2


# ── v2 schemas + instructions describe the dual-type contract ───────────────────────────────────


def test_v2_synth_schemas_are_registered(db):
    from featuregen.overlay.upload.enrich_llm import (
        DocumentSchemaRegistry,
        register_enrichment_schemas,
    )
    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)
    assert reg.schema_for("overlay_table_synth_batch", 2) is not None
    assert reg.schema_for("overlay_table_synth", 2) is not None
    assert reg.schema_for("overlay_table_synth_summary_batch", 2) is not None


def test_instructions_describe_operational_vs_declared():
    for instruction in (ts._INSTRUCTION, ts._SUMMARY_INSTRUCTION, ts._SYNTH_WIDE_INSTRUCTION):
        assert "operational_type" in instruction and "declared_type" in instruction
    assert "name:type" not in ts._SYNTH_WIDE_INSTRUCTION       # the old flat-roster wording is gone
