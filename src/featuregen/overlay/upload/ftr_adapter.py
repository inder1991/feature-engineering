"""FTR glossary adapter (Delivery A1, Task 3a) — the third glossary-shaped reader.

An FTR ``FTR_Column_Mapping.csv`` export is a FIXED 17-column layout (source row id, a
schema-qualified physical FQN, business term + definition, BIAN/FIBO taxonomy, process levels,
synonyms/related terms, a declared SQL type). This module recognizes EXACTLY that layout and turns
it into the same :class:`~featuregen.overlay.upload.glossary_reader.GlossaryUpload` shape the
generic glossary reader produces, so the unchanged validate → graph spine ingests it. Design
decisions follow the round-4/round-5 review resolutions in the A1 plan:

- **Exact fingerprint (#10/#12):** :func:`is_ftr_glossary` is an exact normalized header-MULTISET
  match — a missing, extra, or duplicated header disqualifies the file. A near-miss that still
  carries the FTR-distinctive ``schema.table.column`` header gets a specific diagnostic from
  :func:`ftr_fingerprint_error` (the route turns it into a 400 in Task 3b) instead of silently
  falling through to another reader.
- **Type honesty (#1/#3):** the OPERATIONAL ``CanonicalRow.type`` is ALWAYS ``UNKNOWN_TYPE`` — a
  business glossary is not the physical-type authority. The FTR-declared type is retained only as
  ``GlossaryRecord.declared_type``, validated against a bounded SQL-type token (≤64 chars,
  ``^[a-z0-9 _()]+$`` after lowercasing) so even declared metadata never bypasses the free-text
  controls.
- **Open term_type vocabulary (R5-1, reversing round-4 #7):** term_type is DATA, not a gate. Any
  value — known, new (the real file's ``Regulatory Term``), or blank — ingests, normalized
  (lowercase, spaces→``_``) onto ``GlossaryRecord.term_type``. The ONLY behavioral use anywhere is
  the exact normalized ``measure`` Pass C join-key exclusion (downstream, via ``ColMeta.term_type``).
  ``KNOWN_TERM_TYPES`` documents the observed set for reference; nothing checks against it.
- **Malformed row widths quarantine (R5-6):** a row with cells beyond the 17 headers (an unquoted
  comma shifting every later field one column right — ``csv.DictReader`` files the overflow under
  its ``None`` key) is diverted to the adapter-level quarantine as read, never ingested as parsed.
- **Unresolvable FQNs quarantine via the adapter (R5-7, redoing round-4 M3):** a row whose
  ``schema.table.column`` cell cannot resolve is NOT dropped to an identity-less row (which lost
  the term/domain/taxonomy/term_type/schema/declared_type sidecar in the untagged validate path).
  The adapter emits a tagged ``RowError(adapter="ftr")`` whose reason preserves the raw physical
  FQN + the (already-redacted) record fields, and whose row carries the SANITIZED definition +
  source_row — nothing silently lost, inline repair refused, the FQN visible for the re-upload fix.
- **Parse-time sanitize (#2/#10):** every definition goes through
  :func:`~featuregen.overlay.upload.sanitize.sanitize_definition` (safe facets kept, raw sample
  values stripped or the field blanked); EVERY other uploader free-text field (term name, domain,
  each synonym/related term, joined taxonomy/process paths) is PII-redacted via
  :func:`~featuregen.overlay.upload.sanitize.redact_text`. Quarantined rows carry the SANITIZED
  definition too — nothing raw may reach the durable quarantine.
- **Provenance (#12):** ``source_row`` must be a non-empty integer, unique (as parsed int) across
  the upload; it is stamped on every emitted ``CanonicalRow``/``GlossaryRecord`` for quarantine
  provenance. Rows violating it are quarantined (all members of a duplicate group — fail closed,
  mirroring the duplicate-FQN rule).

Structure mirrors ``read_glossary``'s two passes: Pass 1 parses and indexes the collision keys
(multi-schema folds, multi-schema TABLE spans — R5-4: same table, different columns, different
schemas — duplicate normalized FQNs, source_row ints), diverting malformed-width rows
before their shifted fields can pollute those indexes; Pass 2 emits, diverting bad rows into the
reader-level quarantine whose ``row_index`` starts AT ``len(rows)`` so it can never collide with a
``validate_rows`` index on the ``quarantine_row`` primary key.

Pure module: no DB, no LLM, no route wiring (dispatch is Task 3b).
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.overlay.upload._headers import _norm as _norm_header
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow, RowError
from featuregen.overlay.upload.glossary_reader import (
    GlossaryRecord,
    GlossaryUpload,
    _split_fqn,
    join_path,
    split_list,
)
from featuregen.overlay.upload.object_ref import _norm, normalize_ref
from featuregen.overlay.upload.sanitize import (
    SANITIZER_VERSION,
    redact_text,
    sanitize_definition,
)

# The term_type values observed in real FTR exports, normalized (lowercase, whitespace runs → "_":
# "Reference Data" ⟶ "reference_data"). REFERENCE/DOCS ONLY (R5-1): the vocabulary is OPEN — no
# code checks membership, an unlisted or blank value ingests unchanged. The only behavioral
# consumer of term_type is Pass C's exact-"measure" join-key exclusion, downstream of this module.
KNOWN_TERM_TYPES = frozenset({"measure", "dimension", "code_value", "reference_data",
                              "business_term", "regulatory_term"})

# The exact 17 FTR headers, normalized via the shared header normalizer (`_headers._norm`:
# BOM-strip + lowercase + drop spaces/underscores — dots survive, keeping `schema.table.column`
# distinctive). The fingerprint is the exact MULTISET of these — each present exactly once.
_FTR_HEADERS: tuple[str, ...] = (
    "sourcerow", "schema.table.column", "termname", "descriptionbusinessdefinition", "datadomain",
    "termtype", "relatedbusinessprocessl1", "relatedterms", "relatedbusinessprocessl2",
    "relatedbusinessprocessl3", "synonymsaliases", "bianlevel1", "bianlevel2", "bianlevel3",
    "bianlevel4", "fibolevel1", "datatype",
)
_FTR_MULTISET = Counter(_FTR_HEADERS)
# The header no other reader's file carries — its presence marks a file that MEANT to be FTR.
_FTR_DISTINCTIVE = "schema.table.column"

# Declared-SQL-type bound (resolution #3): even non-operational metadata is validated, never
# free-text. Lowercased first; anything longer than 64 chars or outside the token set is dropped.
_SQL_TYPE_RE = re.compile(r"[a-z0-9 _()]+")
_MAX_SQL_TYPE_LEN = 64

# Quarantine reasons persist: any uploader-supplied value they echo is redacted first, then
# length-bounded (resolution #7's ≤32 — bounding AFTER redaction so truncation cannot split a PII
# token back into an unrecognizable, unredacted fragment).
_REASON_VALUE_BOUND = 32
# The unresolvable-FQN reason echoes the raw physical FQN UNREDACTED (it is structural identity the
# uploader must see to fix the file, not free-text PII) — but still BOUNDED so a pathologically long
# cell cannot bloat the durable quarantine row. Truncated values get a trailing ellipsis.
_MAX_FQN_LEN = 200


@dataclass(frozen=True)
class PreparedFtrUpload:
    """The FTR adapter's typed envelope: the ``GlossaryUpload`` triple plus the sanitize
    provenance the route records in its PARSE stage detail (resolution #6, Task 3b).

    ``sanitized_count`` sums :class:`~featuregen.overlay.upload.sanitize.DefinitionSanitize`
    ``.removed`` across every definition — clauses stripped, fields blanked, PII spans redacted —
    a legacy AGGREGATE kept for continuity. The HONEST breakdown (R5-8) rides beside it:
    ``definitions_stripped`` (non-blanked state ``"stripped"`` — sample clause(s) excised, prose
    kept), ``definitions_suppressed`` (blanked whole, fail-closed — a truthy sanitize ``reason``,
    i.e. an unhandled marker OR a failed PII redaction, the same signal as the R5-3
    ``definition_suppressed`` record flag),
    ``pii_spans_redacted`` (PII spans removed across the DEFINITION redactions of non-blanked
    fields), and ``fields_redacted`` (NON-definition free-text values — term name, domain, each
    synonym/related term, joined taxonomy/process paths — that :func:`redact_text` changed).
    ``input_row_count`` (R5-9) is the number of CSV DATA rows the adapter READ — accepted column
    rows + the table term + every adapter-quarantined row — the honest run-manifest "rows" figure
    (``len(rows)`` drops the table term and the quarantines). ``redaction_version`` is the
    redactor version observed (``None`` only when no text needed redacting at all)."""

    rows: list[CanonicalRow]        # SANITIZED definitions; source_row stamped; type = UNKNOWN_TYPE
    records: list[GlossaryRecord]   # SANITIZED free-text; schema/physical_fqn/declared_type set
    quarantined: list[RowError]     # malformed width, bad/dup FQN, bad/dup source_row, multi-schema
    sanitized_count: int
    sanitizer_version: str
    redaction_version: str | None
    definitions_stripped: int       # R5-8: clause(s) removed, prose kept, NOT blanked
    definitions_suppressed: int     # R5-8: blanked fail-closed (truthy reason: marker OR redaction)
    pii_spans_redacted: int         # R5-8: PII spans removed across definition redactions
    fields_redacted: int            # R5-8: non-definition free-text values redact_text changed
    input_row_count: int            # R5-9: every CSV data row read, quarantines + table term incl.


def is_ftr_glossary(headers: list[str]) -> bool:
    """True iff ``headers`` normalize to EXACTLY the FTR multiset — every FTR header present
    exactly once, nothing missing, nothing extra, nothing duplicated (resolution #10/#12)."""
    return Counter(_norm_header(h) for h in headers) == _FTR_MULTISET


def ftr_fingerprint_error(headers: list[str]) -> str | None:
    """The deterministic near-FTR diagnostic (resolution #10): a file carrying the FTR-distinctive
    ``schema.table.column`` header but NOT the exact FTR multiset gets a message naming the
    missing / extra / duplicate normalized headers, so the uploader fixes the file instead of the
    row-key-less file falling through to a reader that would mangle it. Returns ``None`` when the
    file is exact FTR or not FTR-shaped at all (no distinctive header — other readers may claim it)."""
    got = Counter(_norm_header(h) for h in headers)
    if got == _FTR_MULTISET or _FTR_DISTINCTIVE not in got:
        return None
    missing = sorted(set(_FTR_MULTISET) - set(got))
    extra = sorted(set(got) - set(_FTR_MULTISET))
    duplicate = sorted(h for h, c in got.items() if c > 1)
    parts = []
    if missing:
        parts.append(f"missing [{', '.join(missing)}]")
    if extra:
        parts.append(f"extra [{', '.join(extra)}]")
    if duplicate:
        parts.append(f"duplicate [{', '.join(duplicate)}]")
    return ("near-FTR glossary: the FTR-distinctive 'schema.table.column' header is present but "
            f"the header set is not the exact 17-column FTR layout — {'; '.join(parts)}. "
            "Fix the headers to the exact FTR_Column_Mapping layout and re-upload.")


def _cell(hmap: Mapping[str, str], raw: Mapping[str, object], key: str) -> str:
    col = hmap.get(key)
    val = raw.get(col) if col else None
    return str(val).strip() if val is not None else ""


def _bounded_declared_type(raw: str) -> str:
    """Validate the FTR-declared SQL type as a bounded token (resolution #3): lowercased, kept only
    if ≤64 chars and matching ``^[a-z0-9 _()]+$`` — else dropped to ``""`` (never quarantines: the
    field is non-operational metadata)."""
    dt = raw.strip().lower()
    if dt and len(dt) <= _MAX_SQL_TYPE_LEN and _SQL_TYPE_RE.fullmatch(dt):
        return dt
    return ""


def _reason_value(raw: str) -> str:
    """Render an uploader-supplied value for a persisted quarantine reason: PII-redact FIRST, then
    length-bound (resolution #7)."""
    clean, _ = redact_text(raw)
    return clean[:_REASON_VALUE_BOUND]


def _bounded_fqn(raw_fqn: str) -> str:
    """Cap the raw physical FQN echoed in the unresolvable-FQN quarantine reason to ``_MAX_FQN_LEN``
    (structural identity — bounded, not redacted) so a pathologically long cell cannot bloat the
    durable ``quarantine_row.raw``. A truncated value gets a trailing ellipsis."""
    return raw_fqn if len(raw_fqn) <= _MAX_FQN_LEN else raw_fqn[:_MAX_FQN_LEN] + "…"


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    """One CSV row after Pass 1: parsed identity + sanitized/redacted field values."""

    source_row: str                 # raw cell, stamped on outputs for provenance
    source_row_int: int | None      # parsed id, None when not a valid integer
    definition_suppressed: bool     # R5-3: the sanitizer blanked a DECLARED definition fail-closed
    physical_fqn: str
    schema: str | None
    table: str | None
    column: str | None
    term_name: str
    definition: str                 # SANITIZED (sanitize_definition().clean)
    logical_representation: str     # safe facets from the sanitizer
    semantic_type: str
    domain: str
    term_type_raw: str
    term_type: str                  # normalized (lowercase, spaces→_); OPEN vocabulary (R5-1)
    process_path: str
    related_terms: tuple[str, ...]
    synonyms: tuple[str, ...]
    bian_path: str
    fibo_path: str
    declared_type: str


def read_ftr_glossary(text: str, *, source: str) -> PreparedFtrUpload:
    """Read an exact-fingerprint FTR glossary CSV into the prepared envelope (module docstring)."""
    reader = csv.DictReader(io.StringIO(text))
    hmap = {_norm_header(h): h for h in (reader.fieldnames or [])}

    sanitized_count = 0
    definitions_stripped = 0
    definitions_suppressed = 0
    pii_spans_redacted = 0
    fields_redacted = 0
    input_row_count = 0
    redaction_version: str | None = None

    def _redact(value: str, *, count: bool = True) -> str:
        """redact_text + fold the observed redactor version into the envelope. A CHANGED value
        counts toward ``fields_redacted`` (R5-8) unless ``count=False`` — the quarantine-REASON
        rendering below is not a persisted field value."""
        nonlocal redaction_version, fields_redacted
        clean, version = redact_text(value)
        if redaction_version is None and version is not None:
            redaction_version = version
        if count and clean != value:
            fields_redacted += 1
        return clean

    # Pass 1 — parse + sanitize every row; index the collision keys the emit pass checks:
    # multi-schema folds (mirroring read_glossary — the flat graph drops schema, so only the
    # reader can see two schemas folding onto one (table, column)), duplicate NORMALIZED FQNs
    # (validate_rows would silently dedup two identical rows — the file is malformed, fail closed
    # on BOTH), and source_row ints (uniqueness is judged on the PARSED value: "007" == "7").
    # Malformed-width rows (R5-6) divert straight to `pending` here, BEFORE their shifted fields
    # can be parsed or pollute the collision indexes.
    parsed: list[_ParsedRow] = []
    pending: list[tuple[str, CanonicalRow]] = []   # (message, quarantine row) awaiting an index
    schemas_by_fold: dict[tuple[str, str], dict[str, str]] = {}
    schemas_by_table: dict[str, set[str]] = {}     # normalized table -> its COLUMN rows' schemas
    fqn_counts: Counter[tuple[str, str, str]] = Counter()
    srcrow_counts: Counter[int] = Counter()
    for raw in reader:
        input_row_count += 1
        san = sanitize_definition(_cell(hmap, raw, "descriptionbusinessdefinition"))
        sanitized_count += san.removed
        # R5-8: the honest breakdown. `removed` conflates stripped clauses, blanked fields and PII
        # spans — split it. Suppression is judged by `reason` — the SAME signal as the R5-3
        # definition_suppressed flag below — so ANY fail-closed blank (unhandled_marker OR
        # pii_redaction_failed) counts as suppressed, even when a clause was excised first
        # (state=="stripped" with clean==""). Only a NON-blanked stripped field counts as
        # stripped, and only a NON-blanked field yields a span count: `removed` minus the
        # possible stripped clause. A blanked field never does — no double bookkeeping.
        if san.reason:
            definitions_suppressed += 1
        elif san.state == "stripped":
            definitions_stripped += 1
        if not san.reason:
            pii_spans_redacted += san.removed - (1 if san.state == "stripped" else 0)
        if redaction_version is None and san.redaction_version is not None:
            redaction_version = san.redaction_version
        extra = raw.get(None)
        if extra:
            # R5-6: cells beyond the 17 headers (csv.DictReader's None key) mean an unquoted comma
            # shifted every later field one column right — NOTHING parsed from this row can be
            # trusted, so quarantine it adapter-level as read (identity blanked; the definition
            # cell sanitized so nothing raw persists; source_row kept so the row is locatable).
            # The reason echoes only counts, never cell content — no redaction needed.
            pending.append((
                f"malformed row width: {len(_FTR_HEADERS) + len(extra)} cells, expected "
                f"{len(_FTR_HEADERS)} (an unquoted comma likely shifted fields)",
                CanonicalRow(source=source, table="", column="", type=UNKNOWN_TYPE,
                             definition=san.clean, source_row=_cell(hmap, raw, "sourcerow"))))
            continue
        fqn_raw = _cell(hmap, raw, "schema.table.column")
        schema, table, column = _split_fqn(fqn_raw)
        source_row = _cell(hmap, raw, "sourcerow")
        try:
            source_row_int: int | None = int(source_row)
        except ValueError:
            source_row_int = None
        term_type_raw = _cell(hmap, raw, "termtype")
        row = _ParsedRow(
            source_row=source_row,
            source_row_int=source_row_int,
            # R5-3: `reason` is set ONLY when a non-empty declared definition was blanked
            # fail-closed (unhandled_marker / pii_redaction_failed) — suppressed, not missing.
            definition_suppressed=bool(san.reason),
            physical_fqn=fqn_raw,
            schema=schema, table=table, column=column,
            term_name=_redact(_cell(hmap, raw, "termname")),
            definition=san.clean,
            logical_representation=san.logical_representation,
            semantic_type=san.semantic_type,
            domain=_redact(_cell(hmap, raw, "datadomain")),
            term_type_raw=term_type_raw,
            term_type="_".join(term_type_raw.lower().split()),
            process_path=_redact(join_path([_cell(hmap, raw, "relatedbusinessprocessl1"),
                                            _cell(hmap, raw, "relatedbusinessprocessl2"),
                                            _cell(hmap, raw, "relatedbusinessprocessl3")])),
            related_terms=tuple(t for t in (_redact(item) for item in
                                            split_list(_cell(hmap, raw, "relatedterms"))) if t),
            synonyms=tuple(s for s in (_redact(item) for item in
                                       split_list(_cell(hmap, raw, "synonymsaliases"))) if s),
            bian_path=_redact(join_path([_cell(hmap, raw, "bianlevel1"),
                                         _cell(hmap, raw, "bianlevel2"),
                                         _cell(hmap, raw, "bianlevel3"),
                                         _cell(hmap, raw, "bianlevel4")])),
            fibo_path=_redact(_cell(hmap, raw, "fibolevel1")),
            declared_type=_bounded_declared_type(_cell(hmap, raw, "datatype")),
        )
        parsed.append(row)
        if row.source_row_int is not None:
            srcrow_counts[row.source_row_int] += 1
        if table is not None and schema is not None:
            fqn_counts[(_norm(schema), _norm(table), _norm(column) if column else "")] += 1
            if column is not None:
                fold = (_norm(table), _norm(column))
                schemas_by_fold.setdefault(fold, {}).setdefault(_norm(schema), schema)
                schemas_by_table.setdefault(_norm(table), set()).add(_norm(schema))

    # Pass 2 — emit rows/records, diverting bad rows into the reader-level quarantine. Every
    # quarantined row carries the SANITIZED definition and its raw identity spelling (mirroring
    # read_glossary's raw-valued quarantine rows) with type=UNKNOWN_TYPE (resolution #1 applies to
    # quarantined rows too). Resolution #9's inline-repair refusal covers ALL of these ADAPTER-level
    # quarantine rows (malformed width, unresolvable FQN, dup FQN, bad/dup source_row, multi-schema
    # fold) — they are built as RowError(adapter="ftr") below, and that tag is what
    # resolve_quarantine_row keys the refusal on; the fix is always re-uploading a corrected file.
    rows: list[CanonicalRow] = []
    records: list[GlossaryRecord] = []

    def _quarantine_row(r: _ParsedRow) -> CanonicalRow:
        return CanonicalRow(source=source, table=r.table or "", column=r.column or "",
                            type=UNKNOWN_TYPE, definition=r.definition, source_row=r.source_row)

    for r in parsed:
        if r.table is None:
            # R5-7: an unresolvable FQN quarantines HERE, adapter-tagged, instead of dropping to an
            # identity-less row for validate_rows (which was untagged — inline-repairable — and
            # silently LOST the row's term/domain/taxonomy/term_type/schema/declared_type sidecar).
            # The reason carries the raw physical FQN deliberately unredacted (it is the physical
            # identifier the uploader must see to fix the file) but length-BOUNDED (_bounded_fqn) so
            # a pathologically long cell cannot bloat the durable row — while the record fields it
            # echoes (term_name/domain redacted in Pass 1; term_type redacted here) obey the
            # persistence controls, and the row carries only the SANITIZED definition.
            pending.append((
                f"unresolvable FQN {_bounded_fqn(r.physical_fqn)!r} — term={r.term_name!r} "
                f"type={_redact(r.term_type_raw, count=False)!r} domain={r.domain!r} "
                f"source_row={r.source_row}; fix the FQN and re-upload",
                _quarantine_row(r)))
            continue

        if r.source_row_int is None:
            pending.append((
                f"invalid source_row '{_reason_value(r.source_row)}' — source_row must be a "
                f"non-empty integer unique within the upload", _quarantine_row(r)))
            continue
        if srcrow_counts[r.source_row_int] > 1:
            pending.append((
                f"duplicate source_row {r.source_row_int} — source_row must be unique within the "
                f"upload; every row sharing it is quarantined", _quarantine_row(r)))
            continue

        fqn_key = (_norm(r.schema or ""), _norm(r.table), _norm(r.column) if r.column else "")
        if fqn_counts[fqn_key] > 1:
            pending.append((
                f"duplicate FQN {'.'.join(p for p in fqn_key if p)} declared by multiple rows — "
                f"validation would silently collapse them; keep exactly one row per column and "
                f"re-upload", _quarantine_row(r)))
            continue

        if r.column is not None:
            variants = schemas_by_fold[(_norm(r.table), _norm(r.column))]
            if len(variants) > 1:
                shown = ", ".join(sorted(variants.values(), key=str.lower))
                pending.append((
                    f"schema collision — {_norm(r.table)}.{_norm(r.column)} declared under schemas "
                    f"[{shown}]; the catalog graph is single-schema, so these rows would silently "
                    f"merge into one column — resolve to a single schema and re-upload",
                    _quarantine_row(r)))
                continue

        # R5-4: the fold key above only catches the SAME (table, column) under two schemas — two
        # rows under the same table but DIFFERENT columns evaded it, and the single public.<table>
        # identity would carry one schema on its table node and another on a column. Judge the
        # TABLE across its COLUMN rows (a table term's own schema disagreeing with single-schema
        # columns is the round-4 #5 tail's case, handled at evidence time — not this fence); when
        # the table spans, EVERY row of it — the table term included, its identity is just as
        # ambiguous — fails closed.
        table_schemas = schemas_by_table.get(_norm(r.table), set())
        if len(table_schemas) > 1:
            table = _norm(r.table)
            pending.append((
                f"table {table!r} spans multiple schemas {sorted(table_schemas)!r} — a single "
                f"public.{table} identity cannot carry two schemas; split into one schema per "
                f"upload", _quarantine_row(r)))
            continue

        records.append(GlossaryRecord(
            logical_ref=normalize_ref(source, r.schema, r.table, r.column),
            term_name=r.term_name, definition=r.definition, domain=r.domain,
            synonyms=r.synonyms, bian_path=r.bian_path, fibo_path=r.fibo_path,
            is_table=r.column is None, source_row=r.source_row, term_type=r.term_type,
            process_path=r.process_path, related_terms=r.related_terms,
            schema=r.schema or "", physical_fqn=r.physical_fqn,
            logical_representation=r.logical_representation, semantic_type=r.semantic_type,
            declared_type=r.declared_type, definition_suppressed=r.definition_suppressed))

        if r.column is not None:   # a 2-part table term is a record only, never a CanonicalRow
            rows.append(CanonicalRow(source=source, table=r.table, column=r.column,
                                     type=UNKNOWN_TYPE, definition=r.definition,
                                     source_row=r.source_row))

    # Index the reader-level quarantine AFTER the emitted rows (mirroring read_glossary):
    # validate_rows indexes 0..len(rows)-1 and quarantine_row PKs on (catalog_source, row_index),
    # so the spaces must stay disjoint.
    quarantined = [RowError(len(rows) + j, msg, row, adapter="ftr")
                   for j, (msg, row) in enumerate(pending)]
    return PreparedFtrUpload(rows=rows, records=records, quarantined=quarantined,
                             sanitized_count=sanitized_count,
                             sanitizer_version=SANITIZER_VERSION,
                             redaction_version=redaction_version,
                             definitions_stripped=definitions_stripped,
                             definitions_suppressed=definitions_suppressed,
                             pii_spans_redacted=pii_spans_redacted,
                             fields_redacted=fields_redacted,
                             input_row_count=input_row_count)


def to_glossary_upload(p: PreparedFtrUpload) -> GlossaryUpload:
    """Collapse the prepared envelope to the ``GlossaryUpload`` triple the existing glossary
    ingestion path consumes — the sanitize provenance stays on the envelope for the route's PARSE
    stage detail (resolution #6, Task 3b)."""
    return GlossaryUpload(rows=p.rows, records=p.records, quarantined=p.quarantined)
