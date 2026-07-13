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
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.object_ref import normalize_ref
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

# Synonyms/aliases arrive as a single cell holding several terms. The CSV parser already consumed the
# field-separating commas, so a comma INSIDE a quoted cell is a legit character — split only on the
# list separators a glossary actually uses (`;` and `|`), never on `,`.
_SYNONYM_SEP = re.compile(r"[;|]")


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


@dataclass(frozen=True, slots=True)
class GlossaryUpload:
    """The result of reading a glossary CSV: canonical rows for the validate → graph spine plus the
    per-term semantic sidecars keyed by ``normalize_ref``."""

    rows: list[CanonicalRow] = field(default_factory=list)
    records: list[GlossaryRecord] = field(default_factory=list)


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


def _split_synonyms(value: str) -> tuple[str, ...]:
    return tuple(s.strip() for s in _SYNONYM_SEP.split(value) if s.strip())


def _split_fqn(fqn: str) -> tuple[str | None, str | None, str | None]:
    """Split a ``schema.table.column`` FQN, PRESERVING schema. Returns ``(schema, table, column)``:
    exactly 3 parts → ``(schema, table, column)`` (a COLUMN term); exactly 2 parts → ``(schema, table,
    None)`` (a TABLE term); anything else → ``(None, None, None)`` (no resolvable identity)."""
    parts = [p.strip() for p in fqn.split(".") if p.strip()]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return None, None, None


def read_glossary(text: str, *, source: str) -> GlossaryUpload:
    """Read a glossary CSV into canonical rows + semantic sidecars (see the module docstring)."""
    reader = csv.DictReader(io.StringIO(text))
    fmap = _field_map(list(reader.fieldnames or []))

    rows: list[CanonicalRow] = []
    records: list[GlossaryRecord] = []
    for raw in reader:
        fqn = _cell(fmap, raw, "fqn")
        definition = _cell(fmap, raw, "definition")
        # Optional declared physical type; blank/absent -> UNKNOWN_TYPE (the historical default).
        declared_type = _cell(fmap, raw, "data_type").lower() or UNKNOWN_TYPE
        schema, table, column = _split_fqn(fqn)

        if table is None:
            # Unresolvable FQN — emit an identity-less row so profile-aware validate_rows quarantines
            # it (failure class: invalid FQN / missing identity). No sidecar: a ref needs a table.
            rows.append(CanonicalRow(source=source, table="", column="", type=UNKNOWN_TYPE,
                                     definition=definition))
            continue

        records.append(GlossaryRecord(
            logical_ref=normalize_ref(source, schema, table, column),
            term_name=_cell(fmap, raw, "term_name"), definition=definition,
            domain=_cell(fmap, raw, "domain"), synonyms=_split_synonyms(_cell(fmap, raw, "synonyms")),
            bian_path=_cell(fmap, raw, "bian_path"), fibo_path=_cell(fmap, raw, "fibo_path"),
            is_table=column is None))

        if column is not None:   # a 2-part table term is a record only, never a CanonicalRow
            rows.append(CanonicalRow(source=source, table=table, column=column,
                                     type=declared_type, definition=definition))

    return GlossaryUpload(rows=rows, records=records)
