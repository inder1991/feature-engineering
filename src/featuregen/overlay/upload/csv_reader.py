from __future__ import annotations

import csv
import io

from featuregen.overlay.upload.canonical import CanonicalRow

_ALIASES = {
    "source": {"source", "system"},
    "table": {"table", "tablename"},
    "column": {"column", "columnname", "attribute"},
    "type": {"type", "datatype", "sqltype"},
    "is_grain": {"isgrain", "grain"},
    "as_of": {"asof", "asofcolumn"},
    "definition": {"definition", "description", "comment", "notes"},
}
_TRUE = {"y", "yes", "true", "1"}


def _norm(h: str) -> str:
    return h.strip().lower().replace(" ", "").replace("_", "")


def _field_map(headers: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers:
        n = _norm(h)
        for field, variants in _ALIASES.items():
            if n in variants:
                out[field] = h
    return out


def read_csv_rows(text: str, *, source: str) -> list[CanonicalRow]:
    reader = csv.DictReader(io.StringIO(text))
    fmap = _field_map(reader.fieldnames or [])
    rows: list[CanonicalRow] = []
    for raw in reader:
        def cell(field: str) -> str:
            col = fmap.get(field)
            return (raw.get(col) or "").strip() if col else ""

        def flag(field: str) -> bool:
            return cell(field).lower() in _TRUE

        rows.append(CanonicalRow(
            source=cell("source") or source,
            table=cell("table"), column=cell("column"), type=cell("type"),
            is_grain=flag("is_grain"), as_of=flag("as_of"),
            definition=cell("definition")))
    return rows
