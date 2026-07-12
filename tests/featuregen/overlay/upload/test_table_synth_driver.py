"""Task 6: Pass B synthesis driver + ref-aware, column-validated accept.

`make_ref_accept` validates each serialized `synthesis` against THAT table's real columns (grain
columns + as_of column must exist on the table; as_of basis must be a lag-free enum), mapping a valid
result onto the FACT_VALUE_SCHEMAS shapes (grain `{columns, is_unique}` / availability `{column,
basis}`). An all-empty proposal is an ABSTENTION (`empty_synthesis`), never a guessed grain.
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


def test_grain_column_not_in_table_is_rejected():
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=["ghost"]), "txn")
    assert val is None and reason == "grain_col_not_in_table"


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


def test_bad_as_of_with_no_grain_is_empty_synthesis():
    # Decoupling does NOT resurrect a nothing-proposal: a bad as-of AND no grain -> both absent ->
    # still an abstention (skipped-loud), never a guessed grain.
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=[], as_of_column="ghost", as_of_basis="posted_at"), "txn")
    assert val is None and reason == "empty_synthesis"


def test_abstention_empty_grain_is_skipped_not_guessed():
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=[]), "txn")
    assert val is None and reason == "empty_synthesis"


# --- driver e2e (fake client) -----------------------------------------------------------------------

_STASK = "table_synth"


def test_synthesize_tables_end_to_end(db):
    """A canned batch resolves the valid table; an invalid grain (ghost column) never appears in the
    returned dict — validation runs INSIDE run_batched via the ref-aware accept, not post-filtering."""
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
    assert set(out) == {"txn"}                                 # dim's ghost grain never resolves
    assert out["txn"]["grain"] == {"columns": ["id"], "is_unique": True}
    assert out["txn"]["availability_time"] == {"column": "posted_at", "basis": "posted_at"}
    assert out["txn"]["table_role"] == "fact"
    assert out["txn"]["primary_entity"] == "transaction"
    assert out["txn"]["event_or_snapshot"] == "event"
