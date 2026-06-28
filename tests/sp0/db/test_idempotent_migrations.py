from __future__ import annotations

from sp0.db.migrations import apply_migrations


def test_apply_migrations_twice_succeeds(conn):
    """Migrations claim idempotency (apply_migrations docstring). The session `_dsn` fixture has
    already applied them once; re-applying against the same DB must be a clean no-op, not a
    'relation already exists' failure (plain CREATE TABLE/INDEX/TRIGGER would have raised)."""
    apply_migrations(conn)
    apply_migrations(conn)
    for table in ("events", "timers", "external_commands", "run_transition_table",
                  "feature_versions", "outbox", "legal_holds"):
        row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
        assert row[0] is not None, f"missing table {table} after re-apply"
