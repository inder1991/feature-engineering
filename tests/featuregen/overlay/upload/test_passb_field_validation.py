"""Phase-2 Slice 2 Task 1 — Pass B per-field validation + TOTAL dispositions.

`make_ref_accept` validates every synthesis field INDEPENDENTLY (complete grain checks, the
code-side `table_vocab` role vocabulary, `primary_entity` gated through `known_entities()`,
normalized availability/event fields), drops ONLY the invalid field, and records a disposition
for ALL FIVE fields per resolved synthesis ([F12]: a list of records, `status` in
{accepted, abstained, dropped_invalid}, an absent advisory field == abstained). The vocab is
deliberately NOT a schema enum ([F1] — a schema-side enum would fail the WHOLE synthesis on one
off-vocab role, destroying per-field salvage).
"""
import json

from featuregen.overlay.upload.table_synth import make_ref_accept
from featuregen.overlay.upload.table_vocab import (
    CANONICAL_TABLE_ROLES,
    MAX_GRAIN_COLS,
    normalize_event_or_snapshot,
    normalize_table_role,
)


def _accept(cols):
    disp = []
    return make_ref_accept({"t": set(cols)}, dispositions=disp), disp


def _find(disp, field):
    return next(d for d in disp if d["table"] == "t" and d["field"] == field)


def test_vocab_is_internally_consistent():
    assert normalize_table_role("fact", event_or_snapshot=None) in CANONICAL_TABLE_ROLES  # "fact"
    assert normalize_table_role("dim", event_or_snapshot=None) == "dimension"
    assert normalize_table_role("fact", event_or_snapshot="snapshot") == "snapshot_fact"
    assert normalize_table_role("FACT ", event_or_snapshot="event") == "event_fact"       # strip/lower
    assert normalize_table_role("nonsense", event_or_snapshot=None) is None
    assert normalize_event_or_snapshot(" Event ") == "event"                              # strip/lower


def test_grain_normalized_duplicate_and_invalid_shape():
    accept, disp = _accept(["Id", "amt"])
    # case-variant duplicate must be caught (normalized)
    assert json.loads(accept(json.dumps({"grain_columns": ["id", "ID"]}), "t")[0])["grain"] is None
    assert _find(disp, "grain")["reason"] == "grain_duplicate"
    # a non-string element is an invalid shape, NOT silently filtered
    accept2, disp2 = _accept(["id"])
    assert json.loads(accept2(json.dumps({"grain_columns": ["id", 7]}), "t")[0])["grain"] is None
    assert _find(disp2, "grain")["reason"] == "grain_invalid_shape"


def test_grain_maps_back_to_canonical_table_spelling():
    accept, disp = _accept(["CustomerId"])
    out = json.loads(accept(json.dumps({"grain_columns": ["customerid"]}), "t")[0])
    assert out["grain"]["columns"] == ["CustomerId"]        # canonical table spelling, not the input
    assert _find(disp, "grain")["status"] == "accepted"


def test_grain_over_bound():
    big = [str(i) for i in range(MAX_GRAIN_COLS + 1)]
    accept, disp = _accept(big)
    assert json.loads(accept(json.dumps({"grain_columns": big}), "t")[0])["grain"] is None
    assert _find(disp, "grain")["reason"] == "grain_over_bound"


def test_bad_grain_keeps_role_and_entity():
    accept, disp = _accept(["a"])
    out = json.loads(accept(json.dumps({"grain_columns": ["ghost"], "table_role": "fact",
                                        "primary_entity": "customer", "event_or_snapshot": "event"}),
                            "t")[0])
    assert out["grain"] is None and out["table_role"] == "event_fact"
    assert out["primary_entity"] == "customer"
    assert _find(disp, "grain")["reason"] == "grain_col_not_in_table"


def test_off_vocab_role_and_unregistered_entity_dropped():
    accept, disp = _accept(["a"])
    out = json.loads(accept(json.dumps({"grain_columns": [], "table_role": "wat",
                                        "primary_entity": "Customer"}), "t")[0])
    assert out["table_role"] is None and _find(disp, "table_role")["reason"] == "role_off_vocab"
    assert out["primary_entity"] == "customer"              # "Customer" normalized + registered
    accept2, disp2 = _accept(["a"])
    out2 = json.loads(accept2(json.dumps({"grain_columns": [], "primary_entity": "zzz"}), "t")[0])
    assert out2["primary_entity"] is None
    assert _find(disp2, "primary_entity")["reason"] == "entity_not_registered"


def test_dispositions_are_total_five_fields():
    accept, disp = _accept(["a"])
    accept(json.dumps({"grain_columns": []}), "t")
    fields = {d["field"] for d in disp}
    assert fields == {"grain", "availability_time", "table_role", "primary_entity",
                      "event_or_snapshot"}
    assert _find(disp, "table_role")["status"] == "abstained"   # absent advisory == abstained
    # [F12] record shape: prior_value_staled defaults False; status vocabulary is closed
    assert all(d["prior_value_staled"] is False for d in disp)
    assert all(d["status"] in {"accepted", "abstained", "dropped_invalid"} for d in disp)


# ── [F13] availability + event normalization ────────────────────────────────────────────────────


def test_case_variant_as_of_column_accepted_with_canonical_spelling():
    accept, disp = _accept(["PostedAt", "id"])
    out = json.loads(accept(json.dumps({"grain_columns": ["id"], "as_of_column": " POSTEDAT ",
                                        "as_of_basis": " Posted_At "}), "t")[0])
    assert out["availability_time"] == {"column": "PostedAt", "basis": "posted_at"}
    assert _find(disp, "availability_time")["status"] == "accepted"


def test_bad_as_of_reasons_are_distinct():
    accept, disp = _accept(["id", "posted_at"])
    out = json.loads(accept(json.dumps({"grain_columns": [], "as_of_column": "posted_at",
                                        "as_of_basis": "event_time_plus_lag"}), "t")[0])
    assert out["availability_time"] is None
    assert _find(disp, "availability_time")["reason"] == "basis_not_allowed"
    accept2, disp2 = _accept(["id"])
    out2 = json.loads(accept2(json.dumps({"grain_columns": [], "as_of_column": "ghost",
                                          "as_of_basis": "posted_at"}), "t")[0])
    assert out2["availability_time"] is None
    assert _find(disp2, "availability_time")["reason"] == "as_of_col_not_in_table"


def test_invalid_nonempty_event_or_snapshot_is_dropped_not_abstained():
    accept, disp = _accept(["a"])
    out = json.loads(accept(json.dumps({"grain_columns": [],
                                        "event_or_snapshot": "sometimes"}), "t")[0])
    assert out["event_or_snapshot"] is None
    d = _find(disp, "event_or_snapshot")
    assert d["status"] == "dropped_invalid" and d["reason"] == "event_or_snapshot_off_vocab"
