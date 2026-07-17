"""FTR glossary adapter (Delivery A1, Task 3a) — the third glossary-shaped reader.

An FTR ``FTR_Column_Mapping.csv`` export is a FIXED 17-column layout (source row id, a
schema-qualified physical FQN, business term + definition, BIAN/FIBO taxonomy, process levels,
synonyms/related terms, a declared SQL type). This module recognizes EXACTLY that layout and turns
it into the same :class:`~featuregen.overlay.upload.glossary_reader.GlossaryUpload` shape the
generic glossary reader produces, so the unchanged validate → graph spine ingests it. Design
decisions follow the round-4 review resolutions in the A1 plan:

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
- **Closed term_type vocabulary (#7):** ``TERM_TYPE_VOCAB_V1`` is explicit and versioned in code.
  An unknown value quarantines its row; the value shown in the reason is PII-redacted and
  length-bounded before it can persist.
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
(multi-schema folds, duplicate normalized FQNs, source_row ints); Pass 2 emits, diverting bad rows
into the reader-level quarantine whose ``row_index`` starts AT ``len(rows)`` so it can never
collide with a ``validate_rows`` index on the ``quarantine_row`` primary key.

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

# The closed, versioned term_type vocabulary (resolution #7). Values compare NORMALIZED
# (lowercase, whitespace runs → "_"): "Reference Data" ⟶ "reference_data". Widening the set is a
# NEW version — never a silent edit.
TERM_TYPE_VOCAB_V1 = frozenset({"measure", "dimension", "code_value", "reference_data",
                                "business_term"})

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


@dataclass(frozen=True)
class PreparedFtrUpload:
    """The FTR adapter's typed envelope: the ``GlossaryUpload`` triple plus the sanitize
    provenance the route records in its PARSE stage detail (resolution #6, Task 3b).

    ``sanitized_count`` sums :class:`~featuregen.overlay.upload.sanitize.DefinitionSanitize`
    ``.removed`` across every definition — clauses stripped, fields blanked, PII spans redacted.
    ``redaction_version`` is the redactor version observed (``None`` only when no text needed
    redacting at all)."""

    rows: list[CanonicalRow]        # SANITIZED definitions; source_row stamped; type = UNKNOWN_TYPE
    records: list[GlossaryRecord]   # SANITIZED free-text; schema/physical_fqn/declared_type set
    quarantined: list[RowError]     # bad/dup FQN, bad/dup source_row, unknown term_type, multi-schema
    sanitized_count: int
    sanitizer_version: str
    redaction_version: str | None


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


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    """One CSV row after Pass 1: parsed identity + sanitized/redacted field values."""

    source_row: str                 # raw cell, stamped on outputs for provenance
    source_row_int: int | None      # parsed id, None when not a valid integer
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
    term_type: str                  # normalized (lowercase, spaces→_); vocab-checked in Pass 2
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
    redaction_version: str | None = None

    def _redact(value: str) -> str:
        """redact_text + fold the observed redactor version into the envelope."""
        nonlocal redaction_version
        clean, version = redact_text(value)
        if redaction_version is None and version is not None:
            redaction_version = version
        return clean

    # Pass 1 — parse + sanitize every row; index the collision keys the emit pass checks:
    # multi-schema folds (mirroring read_glossary — the flat graph drops schema, so only the
    # reader can see two schemas folding onto one (table, column)), duplicate NORMALIZED FQNs
    # (validate_rows would silently dedup two identical rows — the file is malformed, fail closed
    # on BOTH), and source_row ints (uniqueness is judged on the PARSED value: "007" == "7").
    parsed: list[_ParsedRow] = []
    schemas_by_fold: dict[tuple[str, str], dict[str, str]] = {}
    fqn_counts: Counter[tuple[str, str, str]] = Counter()
    srcrow_counts: Counter[int] = Counter()
    for raw in reader:
        san = sanitize_definition(_cell(hmap, raw, "descriptionbusinessdefinition"))
        sanitized_count += san.removed
        if redaction_version is None and san.redaction_version is not None:
            redaction_version = san.redaction_version
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

    # Pass 2 — emit rows/records, diverting bad rows into the reader-level quarantine. Every
    # quarantined row carries the SANITIZED definition and its raw identity spelling (mirroring
    # read_glossary's raw-valued quarantine rows) with type=UNKNOWN_TYPE (resolution #1 applies to
    # quarantined rows too — inline repair is refused for FTR rows anyway, resolution #9).
    rows: list[CanonicalRow] = []
    records: list[GlossaryRecord] = []
    pending: list[tuple[str, CanonicalRow]] = []   # (message, raw-valued row) awaiting an index

    def _quarantine_row(r: _ParsedRow) -> CanonicalRow:
        return CanonicalRow(source=source, table=r.table or "", column=r.column or "",
                            type=UNKNOWN_TYPE, definition=r.definition, source_row=r.source_row)

    for r in parsed:
        if r.table is None:
            # Unresolvable FQN — emit an identity-less row so profile-aware validate_rows
            # quarantines it (mirroring read_glossary). No sidecar: a ref needs a table.
            rows.append(CanonicalRow(source=source, table="", column="", type=UNKNOWN_TYPE,
                                     definition=r.definition, source_row=r.source_row))
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

        if r.term_type and r.term_type not in TERM_TYPE_VOCAB_V1:
            shown = ", ".join(sorted(TERM_TYPE_VOCAB_V1))
            pending.append((
                f"unknown term_type '{_reason_value(r.term_type)}' "
                f"(expected one of: {shown}) — TERM_TYPE_VOCAB_V1", _quarantine_row(r)))
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

        records.append(GlossaryRecord(
            logical_ref=normalize_ref(source, r.schema, r.table, r.column),
            term_name=r.term_name, definition=r.definition, domain=r.domain,
            synonyms=r.synonyms, bian_path=r.bian_path, fibo_path=r.fibo_path,
            is_table=r.column is None, source_row=r.source_row, term_type=r.term_type,
            process_path=r.process_path, related_terms=r.related_terms,
            schema=r.schema or "", physical_fqn=r.physical_fqn,
            logical_representation=r.logical_representation, semantic_type=r.semantic_type,
            declared_type=r.declared_type))

        if r.column is not None:   # a 2-part table term is a record only, never a CanonicalRow
            rows.append(CanonicalRow(source=source, table=r.table, column=r.column,
                                     type=UNKNOWN_TYPE, definition=r.definition,
                                     source_row=r.source_row))

    # Index the reader-level quarantine AFTER the emitted rows (mirroring read_glossary):
    # validate_rows indexes 0..len(rows)-1 and quarantine_row PKs on (catalog_source, row_index),
    # so the spaces must stay disjoint.
    quarantined = [RowError(len(rows) + j, msg, row) for j, (msg, row) in enumerate(pending)]
    return PreparedFtrUpload(rows=rows, records=records, quarantined=quarantined,
                             sanitized_count=sanitized_count,
                             sanitizer_version=SANITIZER_VERSION,
                             redaction_version=redaction_version)


def to_glossary_upload(p: PreparedFtrUpload) -> GlossaryUpload:
    """Collapse the prepared envelope to the ``GlossaryUpload`` triple the existing glossary
    ingestion path consumes — the sanitize provenance stays on the envelope for the route's PARSE
    stage detail (resolution #6, Task 3b)."""
    return GlossaryUpload(rows=p.rows, records=p.records, quarantined=p.quarantined)
