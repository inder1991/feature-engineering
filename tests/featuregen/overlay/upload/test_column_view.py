"""Task 3 — ColumnMetadataView + attachable builder (record-source binding, no-crash, fence).

Pure-builder tests: no DB. The sidecar attaches ONLY through the ingest's validated binding map
([F4] — key built from the RECORD source, ``may_attach(None)`` never called, the column never
dropped), and the table-term schema fence is built from ALL parsed non-table records ([F8] —
independent of attachment, so a mismatched table term is fenced even when every column sidecar
is withheld).
"""
from featuregen.overlay.object_identity import ObjectBinding, ObjectIdentityStatus
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_view import build_table_views
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload


def _row(t, c, typ="unknown", defn=""):
    return CanonicalRow(source="s", table=t, column=c, type=typ, definition=defn)


def _rec(ref, **kw):
    base = dict(logical_ref=ref, term_name="T", definition="A settled amount.",
                declared_type="double", term_type="measure", domain="Payments")
    base.update(kw)
    return GlossaryRecord(**base)


def test_types_separate_and_domain_precedence():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee", domain="Payments")])
    v = build_table_views(rows, glossary=g, bindings=None,
                          concepts={}, definitions={}, domains={"txn": "GeneratedDomain"})
    col = v["txn"].columns[0]
    assert col.operational_type == "unknown" and col.declared_type == "double"
    assert col.domain == "Payments"                      # curated glossary domain wins
    assert col.classified_domain == "GeneratedDomain"    # the Pass-A value stays visible


def test_domain_falls_back_to_pass_a_when_no_glossary():
    rows = [_row("txn", "fee")]
    v = build_table_views(rows, glossary=None, bindings=None,
                          concepts={}, definitions={}, domains={"txn": "GeneratedDomain"})
    assert v["txn"].columns[0].domain == "GeneratedDomain"   # Pass-A fallback


def test_technical_upload_fallback_blank_sidecar():
    rows = [_row("txn", "id", typ="unknown")]
    v = build_table_views(rows, glossary=None, bindings=None,
                          concepts={}, definitions={}, domains={})
    col = v["txn"].columns[0]
    assert col.declared_type == "" and col.term_name == "" and col.sidecar_attached is False
    assert col.operational_type == "unknown"


def test_column_kept_but_sidecar_omitted_when_not_attachable():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee")])
    # an AMBIGUOUS binding -> may_attach False -> the COLUMN stays, the sidecar is dropped
    key = "s::public.txn.fee"
    bindings = {key: ObjectBinding(logical_ref=key, status=ObjectIdentityStatus.AMBIGUOUS,
                                   candidates=())}
    v = build_table_views(rows, glossary=g, bindings=bindings,
                          concepts={}, definitions={}, domains={})
    cols = v["txn"].columns
    assert len(cols) == 1                                 # column NOT dropped
    assert cols[0].sidecar_attached is False and cols[0].declared_type == ""


def test_reconciled_facet_withheld():
    rows = [_row("txn", "event_ts")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.event_ts",
        declared_type="timestamp", semantic_type="identifier",
        logical_representation="numeric_string")])
    v = build_table_views(rows, glossary=g, bindings=None, concepts={}, definitions={}, domains={})
    col = v["txn"].columns[0]
    assert col.semantic_type is None and col.logical_representation is None


def test_table_term_schema_mismatch_withholds_definition():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.fee"),
        _rec("s::risk.txn", is_table=True, definition="wrong schema")])
    v = build_table_views(rows, glossary=g, bindings=None, concepts={}, definitions={}, domains={})
    assert v["txn"].table_definition is None


# ── [F4] never crash; key by the RECORD source ──────────────────────────────────────────────────


def test_absent_binding_key_withholds_sidecar_without_crash():
    """A non-None bindings dict with an ABSENT key (ingest sets ``bindings={}`` on a classify
    failure) must withhold the sidecar WITHOUT calling ``may_attach(None)`` — the column is kept."""
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee")])
    v = build_table_views(rows, glossary=g, bindings={},
                          concepts={}, definitions={}, domains={})
    cols = v["txn"].columns
    assert len(cols) == 1                                 # column NOT dropped
    assert cols[0].sidecar_attached is False
    assert cols[0].declared_type == "" and cols[0].term_name == ""


def test_exact_binding_attaches_sidecar():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee")])
    key = "s::public.txn.fee"    # the binding key classify_upload emits (public-scoped, row source)
    bindings = {key: ObjectBinding(logical_ref=None, status=ObjectIdentityStatus.EXACT,
                                   candidates=("s|txn|fee",))}
    v = build_table_views(rows, glossary=g, bindings=bindings,
                          concepts={}, definitions={}, domains={})
    col = v["txn"].columns[0]
    assert col.sidecar_attached is True
    assert col.declared_type == "double" and col.term_name == "T"
    assert col.business_definition == "A settled amount."


def test_cross_source_record_key_uses_record_source():
    """The lookup key is built from the RECORD's parsed source, not the row's: a record declaring
    another source misses the row-source-keyed bindings map -> withheld (cross-source guard)."""
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("other::banking.txn.fee")])
    key = "s::public.txn.fee"    # row-source key IS present and attachable...
    bindings = {key: ObjectBinding(logical_ref=None, status=ObjectIdentityStatus.EXACT,
                                   candidates=("s|txn|fee",))}
    v = build_table_views(rows, glossary=g, bindings=bindings,
                          concepts={}, definitions={}, domains={})
    # ...but the record's own key ("other::public.txn.fee") is absent -> sidecar withheld.
    assert v["txn"].columns[0].sidecar_attached is False


def test_record_without_matching_row_is_ignored():
    """A glossary record naming a column with no CanonicalRow contributes nothing and never
    crashes (the builder iterates rows, so the orphan record is simply never looked up)."""
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee"),
                                           _rec("s::banking.txn.ghost")])
    v = build_table_views(rows, glossary=g, bindings={}, concepts={}, definitions={}, domains={})
    assert [c.column for c in v["txn"].columns] == ["fee"]


def test_unparseable_record_ref_skipped():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("not-a-ref")])
    v = build_table_views(rows, glossary=g, bindings=None, concepts={}, definitions={}, domains={})
    assert v["txn"].columns[0].sidecar_attached is False


# ── [F8] fence built from ALL parsed non-table records ──────────────────────────────────────────


def test_mismatched_table_term_fenced_when_all_sidecars_withheld():
    """The fence uses ALL parsed column records (independent of attachment): a mismatched table
    term must NOT slip through just because every column sidecar was withheld."""
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.fee"),
        _rec("s::risk.txn", is_table=True, definition="wrong schema")])
    v = build_table_views(rows, glossary=g, bindings={},     # every column sidecar withheld
                          concepts={}, definitions={}, domains={})
    assert v["txn"].columns[0].sidecar_attached is False
    assert v["txn"].table_definition is None


def test_matching_table_term_attaches_definition():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn.fee"),
        _rec("s::banking.txn", is_table=True, definition="All card transactions.",
             term_name="Transactions")])
    v = build_table_views(rows, glossary=g, bindings=None,
                          concepts={}, definitions={}, domains={})
    assert v["txn"].table_definition == "All card transactions."
    assert v["txn"].term_name == "Transactions"


def test_table_term_attaches_when_no_column_records_declare_a_schema():
    """No column records at all for the table -> ``column_schemas.get(table) is None`` -> attach."""
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[
        _rec("s::banking.txn", is_table=True, definition="All card transactions.")])
    v = build_table_views(rows, glossary=g, bindings=None,
                          concepts={}, definitions={}, domains={})
    assert v["txn"].table_definition == "All card transactions."


# ── assembly details ────────────────────────────────────────────────────────────────────────────


def test_logical_ref_schema_preserving_when_attached_public_when_not():
    rows = [_row("txn", "fee")]
    g = GlossaryUpload(rows=rows, records=[_rec("s::banking.txn.fee")])
    v = build_table_views(rows, glossary=g, bindings=None,
                          concepts={}, definitions={}, domains={})
    col = v["txn"].columns[0]
    assert col.logical_ref == "s::banking.txn.fee" and col.schema == "banking"
    assert v["txn"].logical_ref == "s::banking.txn"

    v2 = build_table_views(rows, glossary=None, bindings=None,
                           concepts={}, definitions={}, domains={})
    col2 = v2["txn"].columns[0]
    assert col2.logical_ref == "s::public.txn.fee" and col2.schema == "public"
    assert v2["txn"].logical_ref == "s::public.txn"


def test_none_pass_a_maps_normalized_to_empty():
    rows = [_row("txn", "fee")]
    v = build_table_views(rows, glossary=None, bindings=None,
                          concepts=None, definitions=None, domains=None)
    col = v["txn"].columns[0]
    assert col.concept is None and col.drafted_definition is None
    assert col.classified_domain is None and col.domain == ""


def test_business_definition_falls_back_to_draft_and_is_bounded():
    rows = [_row("txn", "fee")]
    h = content_hash(rows[0])
    long_draft = ("A very long drafted definition sentence. " * 30).strip()   # > 600 chars
    v = build_table_views(rows, glossary=None, bindings=None,
                          concepts={h: "fee_amount"}, definitions={h: long_draft}, domains={})
    col = v["txn"].columns[0]
    assert col.concept == "fee_amount"
    assert col.drafted_definition == long_draft            # the raw Pass-A draft stays visible
    assert col.business_definition and len(col.business_definition) <= 600


def test_multiple_tables_indexed_by_name_columns_in_row_order():
    rows = [_row("txn", "fee"), _row("acct", "id"), _row("txn", "amt")]
    v = build_table_views(rows, glossary=None, bindings=None,
                          concepts={}, definitions={}, domains={})
    assert set(v) == {"txn", "acct"}
    assert [c.column for c in v["txn"].columns] == ["fee", "amt"]
    assert [c.column for c in v["acct"].columns] == ["id"]
