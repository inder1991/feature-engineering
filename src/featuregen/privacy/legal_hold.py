from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from psycopg.types.json import Json

from featuregen.contracts import IdentityEnvelope

if TYPE_CHECKING:
    from featuregen.contracts import DbConn


def place_legal_hold(
    conn: DbConn,
    *,
    hold_id: str,
    scope_kind: str,
    scope_ref: str,
    reason: str,
    placed_by: IdentityEnvelope,
) -> None:
    conn.execute(
        "INSERT INTO legal_holds (hold_id, scope_kind, scope_ref, reason, placed_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (hold_id, scope_kind, scope_ref, reason, Json(asdict(placed_by))),
    )


def release_legal_hold(conn: DbConn, hold_id: str) -> None:
    conn.execute("UPDATE legal_holds SET released_at = now() WHERE hold_id = %s", (hold_id,))


def is_under_legal_hold(conn: DbConn, scope_kind: str, scope_ref: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM legal_holds "
        "WHERE scope_kind = %s AND scope_ref = %s AND released_at IS NULL LIMIT 1",
        (scope_kind, scope_ref),
    ).fetchone()
    return row is not None
