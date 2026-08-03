"""1051 — `graph_node` display-projection columns for the fine-grained classification axes.

A migration's audit is blind to the data it will actually meet (`test_migration_1036`'s lesson, and
the recorded "migration audits are blind to legacy data" defect class): `apply_migrations` always
runs on a FRESH database in CI, so a column addition that trips over an existing row passes every
test and fails on the one database that matters. These tests seed a PRE-1051 shape first and then
re-apply the migration SQL exactly as the runner does.

They also pin the D7 reservation reality: 1051 is applied with 1043/1045/1047 already present and
1048-1050 ABSENT from the tree (Release B/C reservations, not written yet), so
the migration may not depend on anything those numbers would create.
"""
from __future__ import annotations

from pathlib import Path

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_NEW_COLUMNS = ("bian_path", "process_path", "sub_domain")


def _migration_1051_sql() -> str:
    return (_MIGRATION_DIR / "1051_graph_node_classification_axes.sql").read_text(encoding="utf-8")


def _columns(db) -> set[str]:
    rows = db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'graph_node'"
    ).fetchall()
    return {r[0] for r in rows}


def _seed_legacy_node(db) -> None:
    """One graph_node row written the way a PRE-1051 ingest wrote it — no knowledge of the three
    new columns at all."""
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "                        data_type, definition, domain) "
        "VALUES ('ftr', 'public.legacy_tbl.legacy_col', 'column', 'legacy_tbl', 'legacy_col', "
        "        'text', 'A pre-1051 column.', 'Compliance')")


def test_1051_adds_the_three_display_columns_over_legacy_rows(db) -> None:
    db.execute("ALTER TABLE graph_node DROP COLUMN IF EXISTS bian_path")
    db.execute("ALTER TABLE graph_node DROP COLUMN IF EXISTS process_path")
    db.execute("ALTER TABLE graph_node DROP COLUMN IF EXISTS sub_domain")
    _seed_legacy_node(db)
    assert not (_columns(db) & set(_NEW_COLUMNS))

    db.execute(_migration_1051_sql())

    assert set(_NEW_COLUMNS) <= _columns(db)
    # The legacy row SURVIVES, un-rewritten, with the three axes honestly NULL (never a fabricated
    # default that would read as "the source said nothing here" vs "the source said this").
    row = db.execute(
        "SELECT definition, domain, bian_path, process_path, sub_domain FROM graph_node "
        "WHERE object_ref = 'public.legacy_tbl.legacy_col'").fetchone()
    assert row == ("A pre-1051 column.", "Compliance", None, None, None)


def test_1051_is_re_runnable_against_an_already_migrated_database(db) -> None:
    """The runner ledgers by name+checksum, but a re-applied file must still be a no-op: the
    `IF NOT EXISTS` guards are what make a repaired/hand-applied database safe."""
    _seed_legacy_node(db)
    db.execute(_migration_1051_sql())
    db.execute(_migration_1051_sql())
    assert set(_NEW_COLUMNS) <= _columns(db)
    assert db.execute(
        "SELECT count(*) FROM graph_node WHERE object_ref = 'public.legacy_tbl.legacy_col'"
    ).fetchone()[0] == 1


def test_1051_applies_with_its_real_neighbours_and_none_of_the_unwritten_reservations() -> None:
    """D7 reservation reality on the FULLY integrated tree: 1043/1045/1046/1047 (Release A),
    1044 (Track-2 merge) and 1048/1049 (Release B Task 7) all exist; 1050 (Release C) and
    1053-1055 (the Phase-G parallel block) do not. 1051 must stand on the tree as it IS —
    a dependency on an unwritten number would be a migration that cannot apply."""
    names = {p.name for p in _MIGRATION_DIR.glob("*.sql")}
    present = {n for n in names
               if n.startswith(("1043_", "1044_", "1045_", "1046_", "1047_", "1048_", "1049_",
                                "1051_"))}
    assert len(present) == 8, sorted(present)
    for absent in ("1050_", "1053_", "1054_", "1055_"):
        assert not [n for n in names if n.startswith(absent)], absent
    # Lexical order is the runner's order (`_sql_file_migrations`), so 1051 lands last of these.
    assert sorted(present)[-1].startswith("1051_")
    statements = [line for line in _migration_1051_sql().splitlines()
                  if line.strip() and not line.lstrip().startswith("--")]
    assert any("graph_node" in line for line in statements)
    # Open vocabularies (D13.2 defers a curated sub-domain list) — no CHECK may reject source data.
    assert not [line for line in statements if "CHECK" in line.upper()]
