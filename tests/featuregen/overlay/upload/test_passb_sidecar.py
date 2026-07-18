"""Task 5 (MF-2) — Pass B receives the COMPLETE FTR glossary sidecar.

`assemble_table_items` now threads a `records: {(table, column): GlossaryRecord}` sidecar map into
each column descriptor, so the glossary columns that HAVE a curated meaning arrive with the declared
type (not `unknown`), the sanitized business definition, and the term_type/domain/process_path
facets — instead of the blank-column-only draft dict that starved exactly those columns before.

The GlossaryRecord below is constructed against its REAL definition in
`overlay/upload/glossary_reader.py` (all keyword args, required fields present); a non-glossary /
technical upload (`records=None`) is byte-for-byte unchanged — the descriptor falls back to `r.type`.
"""
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_llm import _column_profile_ok
from featuregen.overlay.upload.glossary_reader import GlossaryRecord
from featuregen.overlay.upload.table_synth import assemble_table_items


def _row(table, col):
    return CanonicalRow(source="s", table=table, column=col, type="unknown", definition="")


def _rec(table, col, **kw):
    base = dict(logical_ref=f"s::public.{table}.{col}", term_name=f"{col} term",
                definition="A settled amount.", domain="Payments", synonyms=(), bian_path="",
                fibo_path="", is_table=False, term_type="measure",
                process_path="Payments>Settlement", physical_fqn=f"{table}.{col}",
                declared_type="double")
    base.update(kw)
    return GlossaryRecord(**base)


def test_descriptor_carries_full_sidecar():
    rows = [_row("txn", "fee_amt")]
    records = {("txn", "fee_amt"): _rec("txn", "fee_amt")}
    items = assemble_table_items(rows, concepts=None, definitions=None, records=records)
    prof = items[0].metadata["column_profiles"][0]
    assert prof["type"] == "double"                 # declared type, not "unknown"
    assert prof["business_definition"] == "A settled amount."
    assert prof["term_type"] == "measure"
    assert prof["domain"] == "Payments"
    assert prof["process_path"] == "Payments>Settlement"
    assert _column_profile_ok(prof) is True         # egress allows the new keys


def test_no_records_falls_back_to_row_type():
    rows = [_row("txn", "id")]
    items = assemble_table_items(rows, concepts=None, definitions=None, records=None)
    prof = items[0].metadata["column_profiles"][0]
    assert prof["type"] == "unknown"
