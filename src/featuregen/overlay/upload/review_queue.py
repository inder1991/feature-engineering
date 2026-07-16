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
    """Replace the source's quarantine with this upload's errors (whole-source refresh).

    CONTRACT (#33): a caller invokes this only when the new upload SUPERSEDES the queue —
    (a) a successful ingest: its quarantine (INCLUDING an empty one — a clean re-upload of the
        fixed file legitimately clears the queue) now mirrors the file that is in the graph; or
    (b) a NON-ingesting upload (held / all-quarantined / structural-reject) that produced
        quarantine rows: the reviewer must see why ITS rows failed.
    A non-ingesting upload with NO quarantine must NOT call this: the catalog still reflects the
    prior upload, so an empty whole-source refresh would silently wipe a queue the reviewer is
    still working through. The callers in ingest.py guard for that."""
    conn.execute("DELETE FROM quarantine_row WHERE catalog_source = %s", (catalog_source,))
    for e in errors:
        raw = asdict(e.row) if e.row is not None else {}
        if e.sensitivity_floor:
            # Round-3 #4 dismiss-proof floor (see RowError.sensitivity_floor): stored INSIDE the
            # row's own durable record so the resolve path can enforce "declared as at least
            # <tag>" even after the tagged sibling is dismissed (hard-deleted) from the queue.
            raw["sensitivity_conflict_floor"] = list(e.sensitivity_floor)
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
