"""Shared header aliasing + row construction for the format readers (CSV, Excel, ...).

Each reader turns a file into a header row + a sequence of `{header: value}` row dicts; this module
maps aliased headers to canonical fields and builds `CanonicalRow`s — so every format funnels through
one place and the canonical shape stays the single contract.
"""
from __future__ import annotations

from collections.abc import Mapping

from featuregen.overlay.upload.canonical import CanonicalRow

_ALIASES: dict[str, set[str]] = {
    "source": {"source", "system"},
    "table": {"table", "tablename"},
    "column": {"column", "columnname", "attribute"},
    "type": {"type", "datatype", "sqltype"},
    "is_grain": {"isgrain", "grain"},
    "as_of": {"asof", "asofcolumn"},
    "as_of_basis": {"asofbasis", "basis", "availabilitybasis"},
    "definition": {"definition", "description", "comment", "notes"},
    "sensitivity": {"sensitivity", "sensitive", "classification"},
    "joins_to": {"joinsto", "fk", "fktarget", "foreignkey", "references"},
    "cardinality": {"cardinality", "card"},
    "additivity": {"additivity", "additive"},
    "unit": {"unit", "units"},
    "currency": {"currency", "ccy"},
    "entity": {"entity", "businessentity"},
}
_TRUE = {"y", "yes", "true", "1"}
_FALSE = {"n", "no", "false", "0", ""}   # "" (blank/absent) is a valid False


def _norm(h: str) -> str:
    # Strip a UTF-8 BOM too — Excel-exported CSVs prefix the first header with ﻿, which
    # str.strip() does NOT remove, silently unmapping the first column.
    return h.lstrip("﻿").strip().lower().replace(" ", "").replace("_", "")


def field_map(headers: list[str]) -> dict[str, str]:
    """Map each canonical field to the source header that supplies it (unknown headers ignored).

    REJECTS structural header corruption instead of silently last-write-winning (#17): two columns
    that normalize to the same name (a DUPLICATE header — csv.DictReader collapses them and the
    reader can drop a value, e.g. a PII `sensitivity` tag) OR two DISTINCT headers that alias to the
    same canonical field (e.g. `table` + `tablename`) raise ``ValueError``. The caller surfaces it as
    a clear diagnostic (a 400 at the upload boundary) rather than graphing an ambiguous/under-tagged
    row. Unknown/unmapped headers are ignored, so a repeated unrecognized column is harmless."""
    out: dict[str, str] = {}
    seen_norm: dict[str, str] = {}   # normalized header -> the raw header it came from (mapped only)
    for h in headers:
        n = _norm(h)
        matched = next((field for field, variants in _ALIASES.items() if n in variants), None)
        if matched is None:
            continue
        if n in seen_norm:
            raise ValueError(
                f"duplicate header {h!r} (also {seen_norm[n]!r}): a repeated column is silently "
                "collapsed and can drop a value such as a sensitivity tag — give each a distinct name")
        if matched in out:
            raise ValueError(
                f"conflicting headers {out[matched]!r} and {h!r} both map to the '{matched}' field "
                f"— remove one so the {matched} value is unambiguous")
        seen_norm[n] = h
        out[matched] = h
    return out


def build_row(fmap: Mapping[str, str], rowdict: Mapping[str, object], source: str) -> CanonicalRow:
    def cell(field: str) -> str:
        col = fmap.get(field)
        val = rowdict.get(col) if col else None
        return str(val).strip() if val is not None else ""

    def flag(field: str) -> bool:
        token = cell(field).lower()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
        # An unrecognized token was previously coerced to False, silently dropping a possible grain /
        # as-of declaration. Surface it as a parse error instead (#18); the upload boundary 400s it.
        raise ValueError(
            f"invalid boolean for '{field}': {token!r} (expected y/yes/true/1, n/no/false/0, or blank)")

    return CanonicalRow(
        source=cell("source") or source,
        table=cell("table"), column=cell("column"), type=cell("type"),
        is_grain=flag("is_grain"), as_of=flag("as_of"),
        as_of_basis=cell("as_of_basis").lower(),
        definition=cell("definition"), sensitivity=cell("sensitivity").lower(),
        joins_to=cell("joins_to"), cardinality=cell("cardinality"),
        additivity=cell("additivity").lower(), unit=cell("unit"),
        currency=cell("currency"), entity=cell("entity"))
