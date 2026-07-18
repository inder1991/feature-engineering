"""MF-2 — Pass B receives the COMPLETE FTR glossary sidecar (now via the Task-3 views).

The glossary columns that HAVE a curated meaning arrive with the DECLARED type in its OWN field
(`declared_type` — the operational `unknown` stays visible in `operational_type`, never conflated),
the curated business definition, and the term_type/domain/process_path facets. A non-glossary /
technical upload keeps a blank `declared_type` and its physical `operational_type`.

The GlossaryRecord below is constructed against its REAL definition in
`overlay/upload/glossary_reader.py` (all keyword args, required fields present).
"""
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_view import build_table_views
from featuregen.overlay.upload.enrich_llm import _column_profile_ok
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
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


def _assemble(rows, glossary):
    views = build_table_views(rows, glossary=glossary, bindings=None,
                              concepts=None, definitions=None, domains=None)
    return assemble_table_items(views)


def test_descriptor_carries_full_sidecar():
    rows = [_row("txn", "fee_amt")]
    g = GlossaryUpload(rows=rows, records=[_rec("txn", "fee_amt")])
    items = _assemble(rows, g)
    prof = items[0].metadata["column_profiles"][0]
    assert prof["declared_type"] == "double"        # the declared type, in its OWN field
    assert prof["operational_type"] == "unknown"    # the physical type stays visible, unconflated
    assert prof["business_definition"] == "A settled amount."
    assert prof["term_type"] == "measure"
    assert prof["domain"] == "Payments"
    assert prof["process_path"] == "Payments>Settlement"
    assert _column_profile_ok(prof) is True         # egress allows the new keys


def test_no_glossary_falls_back_to_row_type():
    rows = [_row("txn", "id")]
    items = _assemble(rows, None)
    prof = items[0].metadata["column_profiles"][0]
    assert prof["operational_type"] == "unknown" and prof["declared_type"] == ""
    assert "type" not in prof
