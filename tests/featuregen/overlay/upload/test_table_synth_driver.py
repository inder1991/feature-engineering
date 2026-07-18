"""Task 6: Pass B synthesis driver + ref-aware, column-validated accept.

`make_ref_accept` validates each serialized `synthesis` against THAT table's real columns (grain
columns + as_of column must exist on the table; as_of basis must be a lag-free enum), mapping a valid
result onto the FACT_VALUE_SCHEMAS shapes (grain `{columns, is_unique}` / availability `{column,
basis}`). An all-empty proposal is a VALID ABSTENTION (`abstained` — both facts None), never a
guessed grain (MF-3); only unparseable / non-object raw is rejected. Slice 2: an INVALID field
(e.g. a ghost grain column) drops THAT FIELD ONLY — the synthesis still resolves with the
surviving fields (per-field salvage), and advisory fields are vocab-normalized (`fact`+`event` ->
`event_fact`).
`synthesize_tables` drives `run_batched` over the assembled items and returns `{table: synthesis}` for
VALID results only — validation happens INSIDE the harness (ref-aware), so an INVALID synthesis never
reaches the returned dict.
"""
import json

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.enrich_batch import BatchItem
from featuregen.overlay.upload.table_synth import make_ref_accept, synthesize_tables


def _syn(**kw):
    base = {"grain_columns": [], "as_of_column": None, "as_of_basis": None,
            "primary_entity": None, "table_role": None, "event_or_snapshot": None}
    base.update(kw)
    return json.dumps(base, sort_keys=True)


def test_valid_grain_maps_to_fact_shape():
    accept = make_ref_accept({"txn": {"id", "amt", "posted_at"}})
    val, reason = accept(_syn(grain_columns=["id"], as_of_column="posted_at",
                              as_of_basis="posted_at", table_role="fact"), "txn")
    out = json.loads(val)
    assert out["grain"] == {"columns": ["id"], "is_unique": True}   # the proposed CLAIM
    assert out["availability_time"] == {"column": "posted_at", "basis": "posted_at"}
    assert out["table_role"] == "fact"


def test_grain_column_not_in_table_drops_grain_only():
    # Slice 2 (per-field salvage): a ghost grain column drops the GRAIN FIELD only — the synthesis
    # still resolves (abstained: no grain/availability) and keeps its surviving advisory fields,
    # instead of the old whole-item rejection that lost a valid role/entity with it.
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=["ghost"], table_role="reference"), "txn")
    assert val is not None and reason == "abstained"
    out = json.loads(val)
    assert out["grain"] is None and out["availability_time"] is None
    assert out["table_role"] == "reference"


def test_parseable_non_object_json_is_rejected_not_object():
    # "null" / "[]" / "\"x\"" PARSE fine but are not JSON objects — the accept must reject them
    # (`not_object`), never AttributeError on `s.get(...)` inside the validation harness.
    accept = make_ref_accept({"txn": {"id"}})
    assert accept("null", "txn") == (None, "not_object")
    assert accept("[]", "txn") == (None, "not_object")
    assert accept('"x"', "txn") == (None, "not_object")


def test_bad_as_of_col_keeps_a_valid_grain():
    # Whole-branch fix #3: a bad as-of (column not on the table) must drop ONLY the availability —
    # a VALID grain must still come through, not be discarded with the hallucinated as-of.
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=["id"], as_of_column="ghost", as_of_basis="posted_at"),
                         "txn")
    assert reason == "valid"
    out = json.loads(val)
    assert out["grain"] == {"columns": ["id"], "is_unique": True}   # grain survives
    assert out["availability_time"] is None                         # the bad as-of is dropped


def test_bad_as_of_basis_keeps_a_valid_grain():
    # Same decoupling for an invalid basis (a real column, but a non-lag-free basis).
    accept = make_ref_accept({"txn": {"id", "posted_at"}})
    val, reason = accept(_syn(grain_columns=["id"], as_of_column="posted_at",
                              as_of_basis="event_time_plus_lag"), "txn")
    assert reason == "valid"
    out = json.loads(val)
    assert out["grain"] == {"columns": ["id"], "is_unique": True}
    assert out["availability_time"] is None


def test_bad_as_of_with_no_grain_is_a_valid_abstention():
    # MF-3: decoupling does NOT resurrect a guessed grain — but a bad as-of AND no grain is now a
    # VALID ABSTENTION (both facts None), not a whole-item reject. The advisory role/entity survive.
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=[], as_of_column="ghost", as_of_basis="posted_at",
                              table_role="reference"), "txn")
    assert reason == "abstained"
    out = json.loads(val)
    assert out["grain"] is None and out["availability_time"] is None   # zero facts proposed
    assert out["table_role"] == "reference"                            # advisory field retained


def test_empty_grain_is_a_valid_abstention_not_guessed():
    # MF-3: an empty grain_columns is the model ABSTAINING — accepted (no grain guessed), reason
    # "abstained" so _enrichment_outcome counts it resolved-but-abstained, not a stage failure.
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=[]), "txn")
    assert reason == "abstained"
    out = json.loads(val)
    assert out["grain"] is None and out["availability_time"] is None


# --- driver e2e (fake client) -----------------------------------------------------------------------

_STASK = "table_synth"


def test_synthesize_tables_end_to_end(db):
    """A canned batch resolves both tables; dim's ghost grain drops the grain FIELD only (Slice 2
    per-field salvage) — validation runs INSIDE run_batched via the ref-aware accept, not
    post-filtering. Advisory fields come back vocab-normalized (fact+event -> event_fact)."""
    items = [BatchItem("txn", {"table": "txn",
                               "column_profiles": [{"column": "id", "type": "integer"},
                                                   {"column": "posted_at", "type": "timestamp"}]}),
             BatchItem("dim", {"table": "dim",
                               "column_profiles": [{"column": "id", "type": "integer"}]})]
    client = FakeLLM(script={_STASK: FakeResponse(output={"results": [
        {"ref": "txn", "synthesis": {"grain_columns": ["id"], "as_of_column": "posted_at",
                                     "as_of_basis": "posted_at", "table_role": "fact",
                                     "primary_entity": "transaction", "event_or_snapshot": "event"}},
        {"ref": "dim", "synthesis": {"grain_columns": ["ghost"]}},  # names a non-column -> INVALID
    ]})})
    out = synthesize_tables(db, client, items,
                            columns_by_table={"txn": {"id", "posted_at"}, "dim": {"id"}},
                            actor=None)
    assert set(out) == {"txn", "dim"}                          # dim resolves WITHOUT the ghost grain
    assert out["txn"]["grain"] == {"columns": ["id"], "is_unique": True}
    assert out["txn"]["availability_time"] == {"column": "posted_at", "basis": "posted_at"}
    assert out["txn"]["table_role"] == "event_fact"            # "fact" + event -> event_fact
    assert out["txn"]["primary_entity"] == "transaction"
    assert out["txn"]["event_or_snapshot"] == "event"
    assert out["dim"]["grain"] is None                         # the invalid FIELD dropped, not the table
