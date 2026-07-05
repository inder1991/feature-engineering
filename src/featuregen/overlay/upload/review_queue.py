"""Review queue — the human touchpoint the pivot left implicit (no owners to route to).

On each successful ingest, a source's quarantined rows (validation failures) are persisted with their
raw content + reason, replacing the source's prior quarantine (re-evaluated every upload, not sticky).
`list_quarantine` surfaces the pending items for a source; a reviewer fixes the file (or a rule) and
re-uploads, and a now-clean row simply stops appearing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from psycopg.types.json import Jsonb

from featuregen.overlay.upload.canonical import RowError


@dataclass(frozen=True, slots=True)
class QuarantineItem:
    row_index: int
    raw: dict
    reason: str


def persist_quarantine(conn, catalog_source: str, errors: list[RowError]) -> None:
    """Replace the source's quarantine with this upload's errors (whole-source refresh)."""
    conn.execute("DELETE FROM quarantine_row WHERE catalog_source = %s", (catalog_source,))
    for e in errors:
        raw = asdict(e.row) if e.row is not None else {}
        conn.execute(
            "INSERT INTO quarantine_row (catalog_source, row_index, raw, reason) "
            "VALUES (%s, %s, %s, %s)",
            (catalog_source, e.row_index, Jsonb(raw), e.message))


def list_quarantine(conn, catalog_source: str) -> list[QuarantineItem]:
    rows = conn.execute(
        "SELECT row_index, raw, reason FROM quarantine_row WHERE catalog_source = %s "
        "ORDER BY row_index",
        (catalog_source,)).fetchall()
    return [QuarantineItem(row_index=r[0], raw=r[1], reason=r[2]) for r in rows]
