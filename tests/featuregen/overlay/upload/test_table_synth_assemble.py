"""Task 5 — Pass B per-table input assembler (`assemble_table_items`).

Asserts one egress-admissible BatchItem per table, that a CURATED definition rides as a
sample-stripped `business_definition`, and — the M4 egress invariant — that a technical row's raw
`r.definition` free-text NEVER reaches the descriptor.
"""
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash  # the Pass A content-hash key
from featuregen.overlay.upload.enrich_llm import _item_egress_ok
from featuregen.overlay.upload.table_synth import assemble_table_items


def _row(table, column, type_="string", definition="", source="s"):
    # NOTE: the real CanonicalRow requires `source` (no default) and content_hash folds it in, so the
    # factory supplies one. Field order/names mirror overlay/upload/canonical.py::CanonicalRow.
    return CanonicalRow(source=source, table=table, column=column, type=type_, definition=definition,
                        sensitivity="", is_grain=False, as_of=False, as_of_basis="",
                        cardinality="", additivity="", unit="", currency="", entity="",
                        joins_to="")


def test_one_item_per_table_egress_admissible():
    rows = [_row("txn", "id"), _row("txn", "amt"), _row("cust", "cust_id")]
    concepts = {content_hash(rows[1]): "monetary_amount"}
    items = assemble_table_items(rows, concepts=concepts, definitions={})
    assert {it.ref for it in items} == {"txn", "cust"}
    txn = next(it for it in items if it.ref == "txn")
    assert txn.metadata["table"] == "txn"
    assert {d["column"] for d in txn.metadata["column_profiles"]} == {"id", "amt"}
    assert any(d.get("concept") == "monetary_amount" for d in txn.metadata["column_profiles"])
    assert _item_egress_ok(txn.metadata) is True   # <-- the egress contract from Task 3


def test_curated_definition_is_sample_stripped():
    # the CURATED definition (from the sidecar/draft) rides as business_definition, sample-stripped
    rows = [_row("txn", "acct")]
    curated = {content_hash(rows[0]):
               "account number; sample profile is NUMERIC, representative values such as 3708484836801"}
    items = assemble_table_items(rows, concepts={}, definitions=curated)
    desc = items[0].metadata["column_profiles"][0]
    assert "3708484836801" not in desc.get("business_definition", "")   # stripped


def test_uploader_raw_definition_never_egresses():
    # a TECHNICAL row's raw r.definition free-text (a name, a bare id) must NEVER reach the LLM (M4).
    rows = [_row("txn", "cust", definition="belongs to John Q. Public, ssn 123456789")]
    items = assemble_table_items(rows, concepts={}, definitions={})   # no curated definition
    desc = items[0].metadata["column_profiles"][0]
    assert "business_definition" not in desc                          # r.definition dropped entirely
    assert "123456789" not in str(desc) and "John" not in str(desc)


def test_none_concepts_and_definitions_degrade_safely():
    # Pass A stages are savepointed and can fail, leaving concepts/definitions None (ingest guard).
    rows = [_row("txn", "id")]
    items = assemble_table_items(rows, concepts=None, definitions=None)
    assert len(items) == 1
    desc = items[0].metadata["column_profiles"][0]
    assert desc["column"] == "id" and "concept" not in desc
    assert _item_egress_ok(items[0].metadata) is True
