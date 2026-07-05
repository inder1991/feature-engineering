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


def _norm(h: str) -> str:
    # Strip a UTF-8 BOM too — Excel-exported CSVs prefix the first header with ﻿, which
    # str.strip() does NOT remove, silently unmapping the first column.
    return h.lstrip("﻿").strip().lower().replace(" ", "").replace("_", "")


def field_map(headers: list[str]) -> dict[str, str]:
    """Map each canonical field to the source header that supplies it (unknown headers ignored)."""
    out: dict[str, str] = {}
    for h in headers:
        n = _norm(h)
        for field, variants in _ALIASES.items():
            if n in variants:
                out[field] = h
    return out


def build_row(fmap: Mapping[str, str], rowdict: Mapping[str, object], source: str) -> CanonicalRow:
    def cell(field: str) -> str:
        col = fmap.get(field)
        val = rowdict.get(col) if col else None
        return str(val).strip() if val is not None else ""

    def flag(field: str) -> bool:
        return cell(field).lower() in _TRUE

    return CanonicalRow(
        source=cell("source") or source,
        table=cell("table"), column=cell("column"), type=cell("type"),
        is_grain=flag("is_grain"), as_of=flag("as_of"),
        as_of_basis=cell("as_of_basis").lower(),
        definition=cell("definition"), sensitivity=cell("sensitivity").lower(),
        joins_to=cell("joins_to"), cardinality=cell("cardinality"),
        additivity=cell("additivity").lower(), unit=cell("unit"),
        currency=cell("currency"), entity=cell("entity"))
