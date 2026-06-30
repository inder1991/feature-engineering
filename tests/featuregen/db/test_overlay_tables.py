from __future__ import annotations

from featuregen.db.migrations import apply_migrations

_TABLES = (
    "overlay_fact_state",
    "overlay_proposal",
    "overlay_evidence",
    "overlay_fact_dependency",
    "overlay_catalog_object",
)


def test_overlay_tables_exist(conn):
    apply_migrations(conn)
    for table in _TABLES:
        reg = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0]
        assert reg is not None, f"missing table {table}"


def test_overlay_projection_checkpoint_seeded(conn):
    apply_migrations(conn)
    row = conn.execute(
        "SELECT projection_name, checkpoint_seq, head_seq, is_analytics "
        "FROM projection_checkpoints WHERE projection_name='overlay'"
    ).fetchone()
    assert row == ("overlay", 0, 0, False)


def test_overlay_tables_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    n = conn.execute(
        "SELECT count(*) FROM projection_checkpoints WHERE projection_name='overlay'"
    ).fetchone()[0]
    assert n == 1  # ON CONFLICT DO NOTHING — re-apply does not duplicate the row
    idx = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='overlay_fact_dependency_ref_idx'"
    ).fetchone()
    assert idx is not None
