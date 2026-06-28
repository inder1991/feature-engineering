from __future__ import annotations

from psycopg.rows import dict_row

from sp0.contracts import DbConn, Projection
from sp0.projections.runner import _head_seq, projection_lag, rebuild_projection


def set_alias(conn: DbConn, alias: str, projection_name: str) -> None:
    """Point an alias at a projection (upsert)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projection_active_alias (alias, projection_name)
            VALUES (%s, %s)
            ON CONFLICT (alias)
            DO UPDATE SET projection_name = EXCLUDED.projection_name, switched_at = now()
            """,
            (alias, projection_name),
        )


def resolve_projection(conn: DbConn, alias: str) -> str:
    """Return the projection currently behind an alias; if none, the alias itself."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT projection_name FROM projection_active_alias WHERE alias = %s",
            (alias,),
        )
        row = cur.fetchone()
    return alias if row is None else row["projection_name"]


def migrate_projection(conn: DbConn, alias: str, new_projection: Projection) -> None:
    """Build new_projection from global_seq=0 in parallel (the old one still serves reads),
    then switch the alias atomically once the new projection has caught up to head (§3.6)."""
    rebuild_projection(conn, new_projection)
    if projection_lag(conn, new_projection.name) != 0:
        raise RuntimeError(
            f"migration aborted: {new_projection.name} not caught up to head"
        )
    head = _head_seq(conn)
    with conn.cursor() as cur:
        # single-statement atomic read-switch.
        cur.execute(
            """
            INSERT INTO projection_active_alias (alias, projection_name, switched_seq, switched_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (alias)
            DO UPDATE SET projection_name = EXCLUDED.projection_name,
                          switched_seq = EXCLUDED.switched_seq,
                          switched_at = now()
            """,
            (alias, new_projection.name, head),
        )
