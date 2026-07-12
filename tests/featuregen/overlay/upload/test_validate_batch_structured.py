"""Task 4: structured (serialize-through) + ref-aware batch accept.

`validate_batch_results` stays string-typed at the OUTCOME level; an optional `extract(entry) -> str`
hook serializes a STRUCTURED per-item result (e.g. a nested `synthesis` object) to a canonical string,
and a `ref_aware` flag routes `accept(raw, ref)` so per-table column validation runs INSIDE the harness
(INVALID, never accepted-then-post-filtered). Both kwargs are threaded through the real caller chain
`run_batched -> audited_batch_call -> validate_batch_results`; the defaults keep Pass A byte-for-byte.
"""
import json

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload import enrich_batch as eb
from featuregen.overlay.upload.enrich_batch import BatchItem, validate_batch_results


def _accept_json(raw):
    obj = json.loads(raw)
    if not obj.get("grain_columns"):
        return None, "no_grain"
    return raw, "valid"


def test_structured_extract_flows_through_accept():
    items = [BatchItem("txn", {})]
    results = [{"ref": "txn", "synthesis": {"grain_columns": ["id"], "table_role": "fact"}}]
    outcomes = validate_batch_results(
        items, results, "synthesis", _accept_json,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True),
    )
    assert outcomes[0].status == "valid"
    assert json.loads(outcomes[0].value)["grain_columns"] == ["id"]


def test_structured_missing_and_invalid_still_classified():
    items = [BatchItem("txn", {}), BatchItem("dim", {})]
    results = [{"ref": "txn", "synthesis": {"grain_columns": []}}]  # invalid; dim missing
    outcomes = {o.ref: o.status for o in validate_batch_results(
        items, results, "synthesis", _accept_json,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True))}
    assert outcomes["txn"] == "invalid_value"
    assert outcomes["dim"] == "missing"


def test_default_scalar_path_unchanged():
    items = [BatchItem("txn", {})]
    results = [{"ref": "txn", "concept": "amount"}]
    outcomes = validate_batch_results(items, results, "concept", lambda r: (r, "ok"))
    assert outcomes[0].status == "valid" and outcomes[0].value == "amount"


# --- ref_aware: accept is called as accept(raw, ref) -------------------------------------------------

def test_ref_aware_passes_ref_into_accept():
    # A grain column that names a column NOT on this table must classify INVALID inside the harness.
    columns_of = {"txn": {"id", "amount"}, "dim": {"id", "name"}}

    def _accept_ref(raw, ref):
        obj = json.loads(raw)
        bad = [c for c in obj["grain_columns"] if c not in columns_of[ref]]
        return (None, "grain_not_a_column") if bad else (raw, "valid")

    items = [BatchItem("txn", {}), BatchItem("dim", {})]
    results = [
        {"ref": "txn", "synthesis": {"grain_columns": ["id"]}},        # id IS a txn column -> valid
        {"ref": "dim", "synthesis": {"grain_columns": ["amount"]}},    # amount is NOT a dim column -> invalid
    ]
    by = {o.ref: o for o in validate_batch_results(
        items, results, "synthesis", _accept_ref,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True)}
    assert by["txn"].status == "valid"
    assert by["dim"].status == "invalid_value" and by["dim"].reason_codes == ("grain_not_a_column",)


# --- end-to-end through run_batched -> audited_batch_call -> validate_batch_results -----------------

_STASK = "overlay.enrich.table_synth"


def test_run_batched_invokes_structured_extractor_end_to_end(db):
    """Prove the FULL chain threads extract/ref_aware: a canonical JSON string (NOT str(dict)) must
    reach `accept`, with the item's ref. Uses the real audited seam + registered synth batch schema."""
    seen = []

    def _accept(raw, ref):
        seen.append((raw, ref))
        json.loads(raw)                       # canonical JSON; a str(dict) with single quotes would raise
        obj = json.loads(raw)
        return (raw, "valid") if obj.get("grain_columns") else (None, "no_grain")

    items = [BatchItem("txn", {"table": "txn", "columns": ["id", "amount"]})]
    client = FakeLLM(script={_STASK: FakeResponse(output={"results": [
        {"ref": "txn", "synthesis": {"grain_columns": ["id"], "table_role": "fact"}}]})})
    got = eb.run_batched(
        db, client, short="table_synth", task=_STASK,
        prompt_id="overlay_table_synth_batch_v1", schema_id="overlay_table_synth_batch",
        shared_metadata={}, items=items, out_key="synthesis", instruction="Synthesize each table.",
        accept=_accept, actor=None,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True)

    canonical = json.dumps({"grain_columns": ["id"], "table_role": "fact"}, sort_keys=True)
    assert got == {"txn": canonical}                       # the serialized synthesis was cached
    assert seen == [(canonical, "txn")]                    # extractor output (canonical) + ref reached accept
    assert "'" not in seen[0][0]                            # NOT str(dict) (which would use single quotes)


def test_run_batched_ref_aware_skips_single_fallback(db):
    """A ref_aware task must NOT fall back through the flat single seam (no `synthesis` wrapper there,
    accept needs (raw, ref)). The batch returns nothing -> the item is MISSING; the FakeLLM is scripted
    ONLY for the batch prompt_id, so any single-fallback attempt would raise -> proving it is skipped."""
    def _accept(raw, ref):
        return json.loads(raw), "valid"

    items = [BatchItem("txn", {"table": "txn", "columns": ["id"]})]
    client = FakeLLM()
    client.script(task=_STASK, prompt_id="overlay_table_synth_batch_v1",
                  responses=[FakeResponse(output={"results": []})])   # batch resolves nothing
    got = eb.run_batched(
        db, client, short="table_synth", task=_STASK,
        prompt_id="overlay_table_synth_batch_v1", schema_id="overlay_table_synth_batch",
        shared_metadata={}, items=items, out_key="synthesis", instruction="Synthesize each table.",
        accept=_accept, actor=None,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True)
    assert got == {}                                       # unresolved, left uncached; no single fallback
