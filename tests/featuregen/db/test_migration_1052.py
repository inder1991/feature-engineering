"""1052 — `graph_node.data_role` + the table-narrative prose column (`business_context`).

Same discipline as `test_migration_1051`: `apply_migrations` always runs on a FRESH database in CI,
so a column addition that trips over an existing row passes every test and fails on the one
database that matters ("migration audits are blind to legacy data"). These tests seed a PRE-1052
shape first, then re-apply the migration SQL exactly as the runner does.

They also pin the D7 reservation reality: on the fully integrated tree 1043-1049 and 1051 exist;
1050 (Release C) and 1053-1055 (the Phase-G parallel block) do NOT — so 1052 may not depend on
anything those numbers would create.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_NEW_COLUMNS = ("data_role", "business_context", "business_context_decision_id")


def _migration_1052_sql() -> str:
    return (_MIGRATION_DIR / "1052_graph_node_data_role_and_table_prose.sql").read_text(
        encoding="utf-8")


def _columns(db) -> set[str]:
    rows = db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'graph_node'"
    ).fetchall()
    return {r[0] for r in rows}


def _drop_new_columns(db) -> None:
    for column in _NEW_COLUMNS:
        db.execute(f"ALTER TABLE graph_node DROP COLUMN IF EXISTS {column}")


def _seed_legacy_table_node(db) -> None:
    """One TABLE graph_node row written the way a PRE-1052 ingest wrote it: a resolved
    `table_role` display projection already present, and no knowledge of `data_role` at all."""
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, "
        "                        table_role, event_or_snapshot, domain) "
        "VALUES ('ftr', 'public.legacy_map', 'table', 'legacy_map', 'bridge', NULL, 'Compliance')")


def test_1052_adds_the_columns_over_legacy_rows(db) -> None:
    _drop_new_columns(db)
    _seed_legacy_table_node(db)
    assert not (_columns(db) & set(_NEW_COLUMNS))

    db.execute(_migration_1052_sql())

    assert set(_NEW_COLUMNS) <= _columns(db)
    # The legacy row SURVIVES with its existing projection intact and the new columns honestly
    # NULL. NULL, not `unknown`: the migration must not fabricate a classification for a row it
    # never re-derived — `field_resolution` does that, from the evidence, on the next projection.
    row = db.execute(
        "SELECT table_role, domain, data_role, business_context, business_context_decision_id "
        "FROM graph_node WHERE object_ref = 'public.legacy_map'").fetchone()
    assert row == ("bridge", "Compliance", None, None, None)


def test_1052_is_re_runnable_against_an_already_migrated_database(db) -> None:
    """The runner ledgers by name+checksum, but a re-applied file must still be a no-op — the
    `IF NOT EXISTS` guards plus the DROP-then-ADD constraint idiom are what make a repaired or
    hand-applied database safe."""
    _seed_legacy_table_node(db)
    db.execute(_migration_1052_sql())
    db.execute(_migration_1052_sql())
    assert set(_NEW_COLUMNS) <= _columns(db)
    assert db.execute(
        "SELECT count(*) FROM graph_node WHERE object_ref = 'public.legacy_map'"
    ).fetchone()[0] == 1


def test_1052_data_role_check_admits_every_vocabulary_member(db) -> None:
    """The CHECK is the closed `profile_vocab.DataRole` set — derived FROM the enum, so adding a
    member to the enum without migrating is what fails here, not a hand-typed list drifting."""
    from featuregen.overlay.upload.profile_vocab import DataRole

    for member in DataRole:
        db.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, data_role) "
            "VALUES ('ftr', %s, 'table', %s, %s)",
            (f"public.t_{member.value}", f"t_{member.value}", member.value))
    stored = {
        r[0] for r in db.execute(
            "SELECT data_role FROM graph_node WHERE catalog_source = 'ftr' "
            "AND data_role IS NOT NULL").fetchall()}
    assert stored == {m.value for m in DataRole}


def test_1052_data_role_check_refuses_an_off_vocabulary_value(db) -> None:
    with pytest.raises(Exception):   # noqa: B017 — psycopg CheckViolation, driver-typed
        db.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, data_role) "
            "VALUES ('ftr', 'public.bad', 'table', 'bad', 'warehouse')")


def test_1052_applies_with_its_real_neighbours_and_none_of_the_unwritten_reservations() -> None:
    names = {p.name for p in _MIGRATION_DIR.glob("*.sql")}
    present = {n.split("_", 1)[0] for n in names}
    assert {"1043", "1044", "1045", "1046", "1047", "1048", "1049", "1051", "1052"} <= present
    # 1050 belongs to Release C and 1053-1055 to the Phase-G parallel session; none is written,
    # so 1052 stands on what exists.
    assert not ({"1050", "1053", "1054", "1055"} & present)


def test_1052_is_the_only_number_this_stream_allocated() -> None:
    """The consumption step owns 1052 and nothing else (D7). A second file appearing at 1053+
    from this stream would be an unrecorded allocation — the exact class of defect the
    full-filename ledger rule exists to catch."""
    names = sorted(p.name for p in _MIGRATION_DIR.glob("*.sql"))
    beyond = [n for n in names if n.split("_", 1)[0].isdigit() and int(n.split("_", 1)[0]) > 1052]
    assert beyond == []
