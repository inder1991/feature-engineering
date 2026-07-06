from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import conninfo

from featuregen.db import migrations
from featuregen.db.migrations import apply_migrations


@pytest.fixture
def db_empty(_dsn):
    """A genuinely EMPTY database on the same ephemeral cluster — no migrations applied,
    no `schema_migrations` table yet.

    The session `_dsn` fixture already migrated the shared `featuregen_test` DB, so it cannot
    exercise ledger population from scratch. Here we derive admin connection params from the
    resolved `_dsn` (works for both the ephemeral cluster and an external FEATUREGEN_TEST_DSN),
    CREATE a fresh uniquely-named database, hand back a connection to it, then DROP it on
    teardown so the cluster is left clean."""
    params = conninfo.conninfo_to_dict(_dsn)
    admin_dsn = conninfo.make_conninfo(**{**params, "dbname": "postgres"})
    new_db = f"featuregen_ledger_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{new_db}"')
    fresh_dsn = conninfo.make_conninfo(**{**params, "dbname": new_db})
    connection = psycopg.connect(fresh_dsn)
    try:
        yield connection
    finally:
        connection.close()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{new_db}"')


def test_applied_migrations_are_ledgered(db_empty) -> None:
    """apply_migrations must record each migration with a checksum (review MAJOR #8)."""
    apply_migrations(db_empty)
    with db_empty.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] > 0


def test_checksum_drift_is_detected(db_empty) -> None:
    """A recorded migration whose source SQL changed must raise, not silently skip (MAJOR #8)."""
    migrations.apply_migrations(db_empty)
    # Simulate an in-place edit of an already-applied migration.
    with db_empty.cursor() as cur:
        cur.execute(
            "UPDATE schema_migrations SET checksum='deadbeef' "
            "WHERE name=(SELECT name FROM schema_migrations LIMIT 1)"
        )
    with pytest.raises(RuntimeError, match="checksum"):
        migrations.apply_migrations(db_empty)


def test_reapply_skips_all_migrations(db_empty) -> None:
    """On a re-run against an already-migrated DB every migration SKIPs (same checksum): the
    ledger row count and applied_at timestamps are unchanged — apply_migrations stays a safe
    no-op, never re-executing DDL nor re-inserting ledger rows."""
    apply_migrations(db_empty)
    with db_empty.cursor() as cur:
        cur.execute("SELECT count(*), max(applied_at) FROM schema_migrations")
        count_before, applied_before = cur.fetchone()
    apply_migrations(db_empty)
    with db_empty.cursor() as cur:
        cur.execute("SELECT count(*), max(applied_at) FROM schema_migrations")
        count_after, applied_after = cur.fetchone()
    assert count_after == count_before
    assert applied_after == applied_before


def test_sql_file_migrations_fails_loud_when_dir_missing(monkeypatch, tmp_path):
    # A missing migrations dir means a broken build (wheel without package-data), NOT "no migrations".
    # It must raise, never silently return [] (which would apply an empty set to a live DB).
    import pytest

    from featuregen.db import migrations
    monkeypatch.setattr(migrations, "_SQL_MIGRATIONS_DIR", tmp_path / "does-not-exist")
    with pytest.raises(RuntimeError, match="migration"):
        migrations._sql_file_migrations()


def test_sql_migrations_are_actually_present():
    # Guards the packaging contract: the loader finds the shipped .sql files (non-empty).
    from featuregen.db import migrations
    assert len(migrations._sql_file_migrations()) >= 40
