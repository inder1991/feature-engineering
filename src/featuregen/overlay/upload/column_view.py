"""Per-column metadata view for Pass B (Phase-2 Slice 1, Task 3).

Assembles ONE :class:`ColumnMetadataView` per validated ``CanonicalRow`` — the row is ALWAYS
included — and attaches the optional glossary sidecar (term, curated definition, declared SQL
type, taxonomy paths, parser facets) ONLY through the ingest's validated binding map, mirroring
the proven governed path in ``ingest._ingest_glossary_evidence``:

- **[F4] never crash; key by the RECORD source.** The binding-lookup key is built from the
  RECORD's parsed source (``parse_ref(rec.logical_ref)``), NOT the row's — the bindings map is
  row-source-keyed, so a cross-source sidecar simply misses the map and is withheld (the implicit
  cross-source guard the ingest path has). ``may_attach`` is NEVER called with ``None`` (it
  raises; reachable because ingest sets ``bindings={}`` on a classify failure): a non-``None``
  bindings map with an absent key withholds the sidecar, and the column is NEVER dropped from
  Pass B — it keeps a blank sidecar with ``sidecar_attached=False``.
- **[F8] the table-term fence is built from ALL parsed non-table records**, independent of
  ``may_attach``/attachment (mirroring the ingest fence): a table term attaches its
  ``table_definition`` only when ``column_schemas.get(table)`` is absent or exactly
  ``{term_schema}`` — an attached-only fence would let a mismatched table term through whenever
  every column sidecar happens to be withheld.

Pure builder: no DB access, no writes. ``operational_type`` (the row's physical type) and the
glossary ``declared_type`` stay SEPARATE fields; parser facets are reconciled via
:func:`reconcile_profile` exactly as the Phase-1 evidence writer wires it.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.object_identity import ObjectBinding, may_attach
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import bounded_definition, content_hash
from featuregen.overlay.upload.enrich_llm import MAX_DEFINITION_LEN
from featuregen.overlay.upload.glossary_reader import GlossaryRecord, GlossaryUpload
from featuregen.overlay.upload.object_ref import _norm, normalize_ref, parse_ref
from featuregen.overlay.upload.sample_parser import ParsedProfile, reconcile_profile


@dataclass(frozen=True, slots=True)
class ColumnMetadataView:
    """One column's assembled metadata for Pass B. The CanonicalRow's operational identity/type is
    always present; the glossary sidecar fields are blank (``""``/``()``/``None``) with
    ``sidecar_attached=False`` when there is no record or its binding withholds attachment. The
    Pass-A products (``concept``/``drafted_definition``/``classified_domain``) are kept in BOTH
    cases — they are row-derived, not sidecar-derived."""

    source: str
    schema: str
    table: str
    column: str
    logical_ref: str
    operational_type: str          # the row's physical type (CanonicalRow.type) — never the
    declared_type: str             # glossary-DECLARED SQL type; the two stay separate
    term_name: str
    business_definition: str       # curated rec.definition else the Pass-A draft, bounded to 600
    domain: str                    # curated rec.domain FIRST, Pass-A classified_domain fallback
    term_type: str
    process_path: str
    synonyms: tuple[str, ...]
    bian_path: str
    fibo_path: str
    semantic_type: str | None            # reconciled parser facets (withheld -> None)
    logical_representation: str | None
    concept: str | None                  # Pass-A products, keyed by content_hash / table
    drafted_definition: str | None
    classified_domain: str | None
    sidecar_attached: bool


@dataclass(frozen=True, slots=True)
class TableMetadataView:
    """One table's columns plus its (fenced) table-level term. ``table_definition``/``term_name``
    are ``None`` when there is no table term or its declared schema disagrees with the column
    records' ([F8])."""

    source: str
    schema: str
    table: str
    logical_ref: str
    table_definition: str | None
    term_name: str | None
    columns: tuple[ColumnMetadataView, ...]


def _record_indexes(
    glossary: GlossaryUpload | None,
) -> tuple[
    dict[tuple[str, str], tuple[GlossaryRecord, str, str]],
    dict[str, set[str]],
    dict[str, tuple[GlossaryRecord, str]],
]:
    """Parse the glossary's records ONCE into the three lookups the builder needs.

    Returns ``(rec_by_tc, column_schemas, table_terms)``:

    - ``rec_by_tc``: normalized ``(table, column) -> (record, rec_source, rec_schema)`` for every
      parseable non-table record (mirrors ``enrich._records_by_tc``; last record wins).
    - ``column_schemas``: normalized ``table -> {declared schemas}`` from ALL parsed non-table
      records — [F8]: built independent of attachment, mirroring the ingest fence.
    - ``table_terms``: normalized ``table -> (record, term_schema)`` for 2-part table terms.

    An unparseable ``logical_ref`` is skipped (never raises), as on the governed ingest path."""
    rec_by_tc: dict[tuple[str, str], tuple[GlossaryRecord, str, str]] = {}
    column_schemas: dict[str, set[str]] = {}
    table_terms: dict[str, tuple[GlossaryRecord, str]] = {}
    if glossary is None:
        return rec_by_tc, column_schemas, table_terms
    for rec in glossary.records:
        try:
            rec_source, schema, table, column = parse_ref(rec.logical_ref)
        except ValueError:
            continue
        if rec.is_table or column is None:
            if rec.is_table and column is None:
                table_terms[table] = (rec, schema)
            continue
        rec_by_tc[(table, column)] = (rec, rec_source, schema)
        column_schemas.setdefault(table, set()).add(schema)   # [F8] ALL records, not attached-only
    return rec_by_tc, column_schemas, table_terms


def _bounded(text: str) -> str:
    return bounded_definition(text, MAX_DEFINITION_LEN) if text else ""


def _column_view(
    row: CanonicalRow,
    *,
    entry: tuple[GlossaryRecord, str, str] | None,
    bindings: dict[str, ObjectBinding] | None,
    concept: str | None,
    drafted: str | None,
    classified_domain: str | None,
) -> ColumnMetadataView:
    """Assemble one column's view; ``entry`` is its parsed sidecar record (or ``None``)."""
    attached = False
    if entry is not None:
        _rec, rec_source, _schema = entry
        # [F4] mirror of ingest.py:921-929: the lookup key comes from the RECORD's parsed source
        # (bindings are keyed public-scoped under the ROW source, so a cross-source record simply
        # misses), and may_attach is only ever called on a binding that EXISTS. bindings=None means
        # classification did not run for this upload -> the sidecar attaches (trusted path);
        # bindings={} (ingest's classify-failure fallback) withholds EVERY sidecar, crash-free.
        key = normalize_ref(rec_source, None, row.table, row.column)
        binding = None if bindings is None else bindings.get(key)
        attached = bindings is None or (binding is not None and may_attach(binding))

    if attached:
        rec, rec_source, rec_schema = entry
        # Reuse the Phase-1 wiring (ingest._write_glossary_parser_evidence): the reader-carried
        # facets are reconciled against the DECLARED type + column name; a contradicted facet is
        # WITHHELD (None), never asserted wrong.
        reconciled = reconcile_profile(
            ParsedProfile(logical_representation=rec.logical_representation or None,
                          semantic_type=rec.semantic_type or None, computational_type=None,
                          sample_values=(), diagnostic=None),
            declared_type=rec.declared_type, column=row.column,
        )
        return ColumnMetadataView(
            source=rec_source, schema=rec_schema, table=row.table, column=row.column,
            logical_ref=rec.logical_ref,               # schema-preserving sidecar identity
            operational_type=row.type,
            declared_type=rec.declared_type,
            term_name=rec.term_name,
            business_definition=_bounded(rec.definition or (drafted or "")),
            domain=rec.domain or classified_domain or "",   # curated FIRST, Pass-A fallback
            term_type=rec.term_type,
            process_path=rec.process_path,
            synonyms=rec.synonyms,
            bian_path=rec.bian_path,
            fibo_path=rec.fibo_path,
            semantic_type=reconciled.semantic_type,
            logical_representation=reconciled.logical_representation,
            concept=concept,
            drafted_definition=drafted,
            classified_domain=classified_domain,
            sidecar_attached=True,
        )

    # Technical-upload fallback AND the withheld-sidecar case ([F4]): the column is NEVER dropped —
    # it keeps its operational identity, a blank sidecar, and the row-derived Pass-A products.
    return ColumnMetadataView(
        source=row.source, schema="public", table=row.table, column=row.column,
        logical_ref=normalize_ref(row.source, None, row.table, row.column),
        operational_type=row.type,
        declared_type="",
        term_name="",
        business_definition=_bounded(drafted or ""),
        domain=classified_domain or "",
        term_type="", process_path="", synonyms=(), bian_path="", fibo_path="",
        semantic_type=None, logical_representation=None,
        concept=concept,
        drafted_definition=drafted,
        classified_domain=classified_domain,
        sidecar_attached=False,
    )


def build_table_views(
    rows: list[CanonicalRow],
    *,
    glossary: GlossaryUpload | None,
    bindings: dict[str, ObjectBinding] | None,
    concepts: dict[str, str] | None,
    definitions: dict[str, str] | None,
    domains: dict[str, str] | None,
) -> dict[str, TableMetadataView]:
    """Assemble one :class:`TableMetadataView` per table from the validated rows (+ the optional
    glossary sidecar, attached per-column via the ingest's ``bindings``). The outer dict is the
    FTR-convenience index keyed by TABLE NAME; each column view carries its own ``logical_ref``.

    ``concepts``/``definitions`` are the Pass-A maps keyed by ``content_hash(row)``; ``domains``
    is keyed by table name. Each Pass-A stage can fail independently (-> ``None``), so all three
    are normalized to ``{}``. Pure: no DB access."""
    concepts = concepts or {}
    definitions = definitions or {}
    domains = domains or {}
    rec_by_tc, column_schemas, table_terms = _record_indexes(glossary)

    cols_by_table: dict[str, list[ColumnMetadataView]] = {}
    for row in rows:
        if not (row.table and row.column):
            continue   # identity-less row (unresolvable glossary FQN) — mirrors ingest's filter
        table, column = _norm(row.table), _norm(row.column)
        h = content_hash(row)
        view = _column_view(
            row,
            entry=rec_by_tc.get((table, column)),
            bindings=bindings,
            concept=concepts.get(h),
            drafted=definitions.get(h),
            classified_domain=domains.get(table),
        )
        cols_by_table.setdefault(table, []).append(view)

    out: dict[str, TableMetadataView] = {}
    for table, cols in cols_by_table.items():
        table_definition: str | None = None
        table_term_name: str | None = None
        term = table_terms.get(table)
        if term is not None:
            rec, term_schema = term
            declared = column_schemas.get(table)
            # [F8] fence, mirroring ingest.py:1007-1008: the column records are authoritative for
            # the schema — a table term whose schema disagrees attaches NOTHING.
            if not declared or declared == {term_schema}:
                table_definition = _bounded(rec.definition) or None
                table_term_name = rec.term_name or None
        first = cols[0]
        out[table] = TableMetadataView(
            source=first.source, schema=first.schema, table=table,
            logical_ref=normalize_ref(first.source, first.schema, table),
            table_definition=table_definition,
            term_name=table_term_name,
            columns=tuple(cols),
        )
    return out
