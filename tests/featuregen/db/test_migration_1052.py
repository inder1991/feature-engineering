"""1052 — `graph_node.data_role` + the table-narrative prose column (`business_context`).

Same discipline as `test_migration_1051`: `apply_migrations` always runs on a FRESH database in CI,
so a column addition that trips over an existing row passes every test and fails on the one
database that matters ("migration audits are blind to legacy data"). These tests seed a PRE-1052
shape first, then re-apply the migration SQL exactly as the runner does.

They also pin the D7 reservation reality, and pin it to THIS stream's own claim: 1052 is the only
number the consumption step allocated, and 1052 depends on nothing any neighbouring number would
create. What a stream may NOT do is pin its neighbours' allocations — the Phase-G parallel block
already carries 1053 and 1054, and a blanket "nothing exists above 1052" would fail the moment
that session merges, for a stream that neither owns those numbers nor depends on them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_NEW_COLUMNS = ("data_role", "business_context", "business_context_decision_id")
#: Every migration filename THIS stream allocated. The full name, not the number: the ledger keys
#: on name+checksum, and two streams reaching for 1052 with different filenames is precisely the
#: collision the pin below has to see.
_STREAM_MIGRATIONS = ("1052_graph_node_data_role_and_table_prose.sql",)


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
    assert {"1043", "1044", "1045", "1046", "1047", "1048", "1049", "1050", "1051",
            "1052"} <= present
    # 1053-1055 belong to the Phase-G PARALLEL session and are deliberately not asserted either
    # way: 1052 depends on nothing they would create, and pinning their absence would fail the
    # moment that session merges.


def test_1052_is_the_only_number_this_stream_allocated() -> None:
    """The consumption step owns 1052 and nothing else (D7) — asserted as THIS stream's claim.

    Exactly the files it named, exactly once each, and nothing it does not own inside the range it
    allocated. A SECOND `1052_*` file is the unrecorded allocation the full-filename ledger rule
    exists to catch, and that is what this test catches.

    It deliberately says nothing about 1053+. Those belong to the Phase-G parallel session, which
    ALREADY carries `1053_materialization_request.sql` and
    `1054_materialization_compiled_artifact.sql`; a blanket "nothing exists above 1052" is a pin on
    a neighbour's allocation, and it would fail on the merge of a stream this one neither owns nor
    depends on. What keeps each stream honest is its own list, not a fence around everyone else."""
    numbered = [(p.name.split("_", 1)[0], p.name)
                for p in _MIGRATION_DIR.glob("*.sql") if p.name.split("_", 1)[0].isdigit()]
    owned = {name.split("_", 1)[0] for name in _STREAM_MIGRATIONS}
    assert sorted(name for number, name in numbered if number in owned) == sorted(
        _STREAM_MIGRATIONS)
    low, high = min(owned), max(owned)
    assert [name for number, name in numbered
            if low <= number <= high and number not in owned] == []
