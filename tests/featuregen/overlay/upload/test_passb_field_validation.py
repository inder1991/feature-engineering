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
    TABLE_ROLE_ENUM,
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


def test_dispositions_are_total_over_every_field():
    from featuregen.overlay.upload.table_synth import (
        DISPOSITION_FIELDS,
        DISPOSITION_STATUSES,
    )
    accept, disp = _accept(["a"])
    accept(json.dumps({"grain_columns": []}), "t")
    fields = {d["field"] for d in disp}
    # TOTAL over the one constant — five structural fields plus the four profile suggestions.
    assert fields == set(DISPOSITION_FIELDS)
    assert _find(disp, "table_role")["status"] == "abstained"   # absent advisory == abstained
    assert _find(disp, "authority_role")["status"] == "abstained"
    # [F12] record shape: prior_value_staled defaults False; status vocabulary is closed
    assert all(d["prior_value_staled"] is False for d in disp)
    assert all(d["status"] in DISPOSITION_STATUSES for d in disp)


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


# ── [F1] COMPLETE per-field salvage via the REAL path ───────────────────────────────────────────
# The driver validates the WHOLE `{"results": [...]}` envelope with `reg.validate` BEFORE the
# ref-aware accept ever runs, so `as_of_basis` / `event_or_snapshot` (like `table_role`) must be
# BOUNDED STRINGS on the canonical schema — a strict enum there would whole-reject the response on
# one case-variant/off-vocab value, losing a valid grain and making the code-side normalizers
# (strip/lower + `basis_not_allowed` / `event_or_snapshot_off_vocab`) unreachable. These tests run
# `reg.validate` against the REGISTERED schema first, then the accept — not just the direct accept.

_SYNTH_SCHEMA_IDS = ("overlay_table_synth", "overlay_table_synth_batch",
                     "overlay_table_synth_summary_batch")


def _enum_nodes(node):
    if isinstance(node, dict):
        if "enum" in node:
            yield node
        for v in node.values():
            yield from _enum_nodes(v)
    elif isinstance(node, list):
        for v in node:
            yield from _enum_nodes(v)


def test_no_synth_schema_carries_a_strict_enum():
    """No Pass B synth schema (either registered version) may carry a schema-side enum — the
    closed vocabularies live in the PROMPT + `make_ref_accept`, never on the envelope schema."""
    from featuregen.overlay.upload.enrich_llm import _SCHEMAS

    for schema_id in _SYNTH_SCHEMA_IDS:
        for version in (1, 2):
            assert list(_enum_nodes(_SCHEMAS[(schema_id, version)])) == [], (
                f"{schema_id} v{version} carries a strict enum — whole-rejects on one "
                "off-vocab field value, destroying per-field salvage")


def _real_path(db, synthesis: dict, cols):
    """The REAL Pass B validation order: `reg.validate` over the whole batch envelope (schema v2 —
    the version `synthesize_tables` requests) FIRST, then the ref-aware accept on the item."""
    from featuregen.documents.registry import DocumentSchemaRegistry
    from featuregen.overlay.upload.enrich_llm import register_enrichment_schemas

    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)
    # must NOT raise — a field-level vocab violation can never whole-reject the envelope
    reg.validate("overlay_table_synth_batch", 2, {"results": [{"ref": "t", "synthesis": synthesis}]})
    disp: list[dict] = []
    accept = make_ref_accept({"t": set(cols)}, dispositions=disp)
    out, _verdict = accept(json.dumps(synthesis), "t")
    assert out is not None
    return json.loads(out), disp


def test_case_variant_event_or_snapshot_salvaged_via_real_path(db):
    out, disp = _real_path(db, {"grain_columns": ["id"], "event_or_snapshot": " Event "}, {"id"})
    assert out["grain"] == {"columns": ["id"], "is_unique": True}   # grain preserved
    assert out["event_or_snapshot"] == "event"                      # normalized, not whole-rejected
    assert _find(disp, "event_or_snapshot")["status"] == "accepted"


def test_off_vocab_event_or_snapshot_drops_field_only_via_real_path(db):
    out, disp = _real_path(db, {"grain_columns": ["id"], "event_or_snapshot": "sometimes"}, {"id"})
    assert out["grain"] == {"columns": ["id"], "is_unique": True}   # grain KEPT
    assert out["event_or_snapshot"] is None
    d = _find(disp, "event_or_snapshot")
    assert d["status"] == "dropped_invalid" and d["reason"] == "event_or_snapshot_off_vocab"


def test_case_variant_as_of_basis_salvaged_via_real_path(db):
    out, disp = _real_path(db, {"grain_columns": ["id"], "as_of_column": "posted_at",
                                "as_of_basis": " Posted_At "}, {"id", "posted_at"})
    assert out["grain"] == {"columns": ["id"], "is_unique": True}
    assert out["availability_time"] == {"column": "posted_at", "basis": "posted_at"}
    assert _find(disp, "availability_time")["status"] == "accepted"


def test_off_vocab_as_of_basis_drops_availability_only_via_real_path(db):
    out, disp = _real_path(db, {"grain_columns": ["id"], "as_of_column": "posted_at",
                                "as_of_basis": "event_time_plus_lag"}, {"id", "posted_at"})
    assert out["grain"] == {"columns": ["id"], "is_unique": True}
    assert out["availability_time"] is None
    assert _find(disp, "availability_time")["reason"] == "basis_not_allowed"


def test_hallucinated_crosswalk_role_is_salvaged_as_bridge_not_dropped(db):
    """[F5] A DELIBERATE pin on a Pass-B behaviour change nobody asked Pass B for.

    The profile program added ``crosswalk`` to ``table_vocab._ROLE_ALIASES`` so a human profile edit
    could SAY "crosswalk" while the stored evidence vocabulary stayed the legacy ``bridge``. Pass B
    shares that ONE normalizer, so the salvage path changed silently with it: the PROMPT does not
    offer the word (it is absent from the prompt-interpolated ``TABLE_ROLE_ENUM`` — a Pass-B
    ``crosswalk`` is therefore a HALLUCINATION), yet what used to drop with ``role_off_vocab`` is now
    ACCEPTED and written as ``bridge`` table_role evidence.

    Judged acceptable — ``bridge`` IS what a model means by "crosswalk", and an input alias can never
    widen ``CANONICAL_TABLE_ROLES`` — but it must be a visible, pinned decision rather than a
    side effect of a shared constant. Change this test deliberately or not at all."""
    assert "crosswalk" not in TABLE_ROLE_ENUM       # the prompt never offers the word
    out, disp = _real_path(db, {"grain_columns": ["id"], "table_role": " Crosswalk "}, {"id"})
    assert out["table_role"] == "bridge"            # accepted + canonicalized, NOT role_off_vocab
    assert _find(disp, "table_role")["status"] == "accepted"
    assert out["grain"] == {"columns": ["id"], "is_unique": True}   # salvage intact either way


def test_a_dimension_table_with_a_customer_grain_proposes_primary_entity_customer():
    """Richness Task 3 Step 6 regression: `customer` IS in the entity registry, so a dimension
    table's `primary_entity=customer` proposal RESOLVES (accepted), never `entity_not_registered`
    — the exact live shape of `bo_cib_customer` (dimension, grain [business_dt, cust_num])."""
    accept, disp = _accept(["business_dt", "cust_num"])
    raw = json.dumps({"grain_columns": ["business_dt", "cust_num"], "as_of_column": None,
                      "as_of_basis": None, "table_role": "dimension",
                      "primary_entity": "customer", "event_or_snapshot": "snapshot"})
    out, verdict = accept(raw, "t")
    assert verdict == "valid"
    parsed = json.loads(out)
    assert parsed["primary_entity"] == "customer"
    assert parsed["table_role"] == "dimension"
    assert _find(disp, "primary_entity")["status"] == "accepted"
