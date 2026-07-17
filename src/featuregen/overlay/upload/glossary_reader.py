"""Glossary reader — the second file-ingestion entry (spec §U).

A business glossary (a BIAN/FIBO term map like the FTR ``FTR_Column_Mapping.csv``) is NOT a technical
schema export: it carries MEANING (a business term, its definition, taxonomy paths) keyed to a
schema-qualified physical column, but declares no physical type. Under the unified-ingestion model it
reduces to the same ``(rows, SourceCapabilityProfile)`` as a technical CSV — the profile
(``FTR_GLOSSARY_PROFILE``), not a special pipeline, is what differs. This reader turns a glossary CSV
into two things fed to the SAME validate → graph spine as a technical upload:

- ``rows: list[CanonicalRow]`` — one per COLUMN-level term (a 3-part ``schema.table.column`` FQN). A
  glossary declares no physical type by default, so the row's ``type`` is ``UNKNOWN_TYPE`` UNLESS the
  file supplies an optional ``data_type`` column — a DECLARED (not attested) value, since a structural
  source (OpenMetadata / DDL) stays the stronger authority. Profile-aware ``validate_rows`` accepts
  either under the glossary profile; a 2-part table term is NOT emitted as a column, and an unresolvable
  FQN yields an identity-less row so validation quarantines it.
- ``records: list[GlossaryRecord]`` — a semantic sidecar per resolvable term, keyed by the
  schema-preserving ``normalize_ref`` (spec §5.1), carrying the meaning fields the evidence machinery
  attaches in a later task. Schema is preserved HERE (the flat ``CanonicalRow``/legacy graph is
  ``public``-scoped); the sidecar's ``logical_ref`` is the schema-preserving identity.

Detection reuses ``source_profile.profile_for_upload`` (the single header-signature decision) — this
module adds NO third copy of glossary-vs-technical detection.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from featuregen.overlay.upload._headers import _norm as _norm_header
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow, RowError
from featuregen.overlay.upload.object_ref import _norm, normalize_ref
from featuregen.overlay.upload.sample_parser import parse_sample_profile
from featuregen.overlay.upload.source_profile import (
    FTR_GLOSSARY_PROFILE,
    profile_for_upload,
)

# Header aliasing for the glossary's columns — mirrors `_headers._ALIASES` (its keys map to the
# CanonicalRow, ours to the glossary sidecar), and normalizes via the SAME `_norm` the reader/profile
# use (BOM-strip + lowercase + drop spaces/underscores). The FQN column deliberately never aliases to
# `column`/`table` — those are the technical row-key headers whose ABSENCE selects the glossary profile.
_ALIASES: dict[str, set[str]] = {
    "fqn": {"physicalname", "physicalcolumn", "columnfqn", "fqn", "fullyqualifiedname",
            "objectpath", "mappedcolumn", "sourcecolumn", "technicalname", "physicalpath"},
    "term_name": {"businessterm", "term", "termname", "glossaryterm", "businessname"},
    "definition": {"descriptionbusinessdefinition", "businessdefinition", "definition",
                   "description", "meaning"},
    "domain": {"datadomain", "domain", "subjectarea"},
    "synonyms": {"synonyms", "synonym", "aliases", "alias", "alsoknownas"},
    "bian_path": {"bianpath", "bian"},
    "fibo_path": {"fibopath", "fibo"},
    # Optional physical type: a glossary MAY declare the source column's SQL type. It is a DECLARED
    # (not attested) structural value — used when present, else UNKNOWN_TYPE — so a structural source
    # (OpenMetadata / DDL) stays the stronger authority and reconciles on drift. Header names mirror
    # the technical reader's `type` aliases so the same column name works in either file kind.
    "data_type": {"datatype", "type", "sqltype", "physicaltype", "columntype"},
}


@dataclass(frozen=True, slots=True)
class GlossaryRecord:
    """The semantic sidecar for one glossary term, keyed by the schema-preserving ``logical_ref``
    (``normalize_ref``). ``is_table`` marks a 2-part (table-level) term — one with no column segment."""

    logical_ref: str
    term_name: str
    definition: str
    domain: str = ""
    synonyms: tuple[str, ...] = ()
    bian_path: str = ""
    fibo_path: str = ""
    is_table: bool = False
    # FTR-adapter fields (A1) — ALL defaulted so existing constructors and the generic glossary
    # reader above keep working unchanged; only the FTR adapter populates them.
    source_row: str = ""              # original file row id, carried for quarantine provenance
    term_type: str = ""               # closed-vocab term class (measure/dimension/...), normalized
    process_path: str = ""            # joined related_business_process_l1..3
    related_terms: tuple[str, ...] = ()
    schema: str = ""                  # real (pre-flatten) schema segment of the declared FQN
    physical_fqn: str = ""            # the raw schema.table.column as declared in the file
    # The FTR-declared SQL type, retained as NON-operational metadata (round-4 resolution #1): the
    # operational `CanonicalRow.type` stays UNKNOWN_TYPE under the FTR adapter (a business glossary
    # is not the physical-type authority). Validated + bounded by the adapter (resolution #3).
    declared_type: str = ""
    # SAFE parser facets derived from a recognized sample clause (never the raw sample values) —
    # populated by BOTH readers (the FTR adapter via the sanitizer BEFORE stripping; the generic
    # reader at read time) and consumed as PARSER evidence at ingest, which never re-parses the
    # definition (Task 7 / review #4 — the FTR definition arrives already sample-stripped).
    logical_representation: str = ""
    semantic_type: str = ""
    # R5-3: True when the uploader DID declare a definition but the sanitizer blanked it FAIL-CLOSED
    # (an unhandled data marker survived the strip, or PII redaction failed). A suppressed
    # definition is NOT "missing": `enrich.draft_definitions` must skip it, so it stays empty
    # pending review instead of being silently overwritten by LLM text with no governance decision.
    definition_suppressed: bool = False


@dataclass(frozen=True, slots=True)
class GlossaryUpload:
    """The result of reading a glossary CSV: canonical rows for the validate → graph spine plus the
    per-term semantic sidecars keyed by ``normalize_ref``.

    ``quarantined`` (#9) carries the rows the READER itself failed closed on — a multi-schema fold
    collision, detected here because the flat ``CanonicalRow`` drops the schema so validation can no
    longer see it. ``ingest_upload`` merges these into the upload's quarantine (the review queue)
    alongside validation failures. Each ``row_index`` starts AT ``len(rows)`` so it can never collide
    with a ``validate_rows`` index (``0..len(rows)-1``) on the ``quarantine_row`` primary key."""

    rows: list[CanonicalRow] = field(default_factory=list)
    records: list[GlossaryRecord] = field(default_factory=list)
    quarantined: list[RowError] = field(default_factory=list)


def is_glossary_csv(headers: list[str]) -> bool:
    """True iff ``headers`` select the glossary profile. Reuses the SINGLE header-signature decision in
    ``source_profile.profile_for_upload`` — no third copy of glossary-vs-technical detection."""
    return profile_for_upload(headers) is FTR_GLOSSARY_PROFILE


def _field_map(headers: list[str]) -> dict[str, str]:
    """Map each glossary field to the source header that supplies it (unknown headers ignored)."""
    out: dict[str, str] = {}
    for h in headers:
        n = _norm_header(h)
        for field_name, variants in _ALIASES.items():
            if n in variants:
                out[field_name] = h
    return out


def _cell(fmap: Mapping[str, str], raw: Mapping[str, object], field_name: str) -> str:
    col = fmap.get(field_name)
    val = raw.get(col) if col else None
    return str(val).strip() if val is not None else ""


def join_path(parts: list[str], sep: str = " / ") -> str:
    """Join ordered taxonomy levels (e.g. ``bian_level_1..4``) into one path string, dropping blank
    or whitespace-only levels so a sparse hierarchy renders without dangling separators. One of the
    whitelisted glossary transforms (spec: `copy`/`split_fqn`/`join_path`/`split_list`)."""
    return sep.join(p.strip() for p in parts if p.strip())


def split_list(value: str, delimiters: tuple[str, ...] = (";", "|")) -> tuple[str, ...]:
    """Split a single-cell list on ANY of ``delimiters``, stripping items and dropping empties. A
    list cell (synonyms, related terms) holds several values in one field: the CSV parser already
    consumed the field-separating commas, so a comma INSIDE a quoted cell is a legit character —
    the defaults are the list separators a glossary actually uses (``;`` and ``|``), never ``,``."""
    sep = re.compile("[" + "".join(re.escape(d) for d in delimiters) + "]")
    return tuple(s.strip() for s in sep.split(value) if s.strip())


def _split_synonyms(value: str) -> tuple[str, ...]:
    return split_list(value)


def _split_fqn(fqn: str) -> tuple[str | None, str | None, str | None]:
    """Split a ``schema.table.column`` FQN, PRESERVING schema. Returns ``(schema, table, column)``:
    exactly 3 parts → ``(schema, table, column)`` (a COLUMN term); exactly 2 parts → ``(schema, table,
    None)`` (a TABLE term); anything else → ``(None, None, None)`` (no resolvable identity).

    An EMPTY component (``schema..column``, ``.a.b``, ``a.b.``) is detected BEFORE the arity check
    and rejects the whole FQN (#26): filtering empties first would silently REINTERPRET a malformed
    3-part FQN as a valid-looking 2-part TABLE term instead of quarantining it."""
    parts = [p.strip() for p in fqn.split(".")]
    if any(not p for p in parts):
        return None, None, None
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return None, None, None


def read_glossary(text: str, *, source: str) -> GlossaryUpload:
    """Read a glossary CSV into canonical rows + semantic sidecars (see the module docstring)."""
    reader = csv.DictReader(io.StringIO(text))
    fmap = _field_map(list(reader.fieldnames or []))

    # Pass 1 — parse every row and map each COLUMN term's fold key (normalized table.column — the
    # identity the schema-dropping CanonicalRow/graph collapses to) to its distinct schemas (#9).
    # The flat graph ref hardcodes `public.` (graph._column_ref), so two column terms from DIFFERENT
    # schemas sharing (table, column) would silently fold into ONE graph node (last-writer-wins).
    # Detection must happen HERE: the schema is dropped from the CanonicalRow, so no later stage can
    # see the collision. Schemas compare NORMALIZED (case/padding variants of one schema are one
    # schema); the first-seen raw spelling is kept for the reviewer-facing message.
    parsed = []   # (raw, definition, declared_type, schema, table, column) per CSV row
    schemas_by_fold: dict[tuple[str, str], dict[str, str]] = {}
    for raw in reader:
        definition = _cell(fmap, raw, "definition")
        # Optional declared physical type; blank/absent -> UNKNOWN_TYPE (the historical default).
        declared_type = _cell(fmap, raw, "data_type").lower() or UNKNOWN_TYPE
        schema, table, column = _split_fqn(_cell(fmap, raw, "fqn"))
        parsed.append((raw, definition, declared_type, schema, table, column))
        if table is not None and column is not None and schema is not None:
            fold = (_norm(table), _norm(column))
            schemas_by_fold.setdefault(fold, {}).setdefault(_norm(schema), schema)

    # Pass 2 — emit rows/records, diverting every column term whose fold key spans >1 schema into
    # the reader-level quarantine (fail-closed: no CanonicalRow, no sidecar — a quarantined identity
    # must not be graphed or receive evidence). A single schema per (table, column) — one schema, or
    # the same schema repeated — ingests exactly as before.
    rows: list[CanonicalRow] = []
    records: list[GlossaryRecord] = []
    collisions: list[tuple[str, CanonicalRow]] = []   # (message, raw-valued row) pending an index
    for raw, definition, declared_type, schema, table, column in parsed:
        if table is None:
            # Unresolvable FQN — emit an identity-less row so profile-aware validate_rows quarantines
            # it (failure class: invalid FQN / missing identity). No sidecar: a ref needs a table.
            rows.append(CanonicalRow(source=source, table="", column="", type=UNKNOWN_TYPE,
                                     definition=definition))
            continue

        if column is not None:
            variants = schemas_by_fold[(_norm(table), _norm(column))]
            if len(variants) > 1:
                shown = ", ".join(sorted(variants.values(), key=str.lower))
                collisions.append((
                    f"schema collision — {_norm(table)}.{_norm(column)} declared under schemas "
                    f"[{shown}]; the catalog graph is single-schema, so these rows would silently "
                    f"merge into one column — resolve to a single schema and re-upload",
                    CanonicalRow(source=source, table=table, column=column,
                                 type=declared_type, definition=definition)))
                continue

        # SAFE parser facets, captured at read time so the record CARRIES them (Task 7 / review #4):
        # evidence-time no longer re-parses the definition (an adapter may have stripped its sample
        # clause). The generic reader does not strip, so parsing here is behaviour-preserving.
        profile = parse_sample_profile(definition)
        records.append(GlossaryRecord(
            logical_ref=normalize_ref(source, schema, table, column),
            term_name=_cell(fmap, raw, "term_name"), definition=definition,
            domain=_cell(fmap, raw, "domain"), synonyms=_split_synonyms(_cell(fmap, raw, "synonyms")),
            bian_path=_cell(fmap, raw, "bian_path"), fibo_path=_cell(fmap, raw, "fibo_path"),
            is_table=column is None,
            logical_representation=profile.logical_representation or "",
            semantic_type=profile.semantic_type or ""))

        if column is not None:   # a 2-part table term is a record only, never a CanonicalRow
            rows.append(CanonicalRow(source=source, table=table, column=column,
                                     type=declared_type, definition=definition))

    # Index the reader-level quarantine AFTER the emitted rows: validate_rows indexes 0..len(rows)-1,
    # and quarantine_row PKs on (catalog_source, row_index), so the spaces must stay disjoint.
    quarantined = [RowError(len(rows) + j, msg, row) for j, (msg, row) in enumerate(collisions)]
    return GlossaryUpload(rows=rows, records=records, quarantined=quarantined)
