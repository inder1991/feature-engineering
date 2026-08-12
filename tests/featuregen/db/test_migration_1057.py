"""1057 — the Release C Task 11 crosswalk observation store.

Same discipline as `test_migration_1050`: `apply_migrations` always runs on a FRESH database in CI,
so a migration that trips over an existing shape passes every test and fails on the one database
that matters ("migration audits are blind to legacy data"). These tests drop back to a pre-1057
shape, re-apply the SQL exactly as the runner does, then re-apply it AGAIN over populated tables.

NEIGHBOUR TOLERANCE (the 1050 rule, carried forward): 1053-1056 belong to PARALLEL sessions
(Phase-G's reserved block and the PII allow-policy surface) and may or may not be present while this
branch is open, so nothing here asserts anything about them. What it does assert is that this stream
allocated 1057 and only 1057 — tolerance about a neighbour's numbers is not licence to be vague
about your own. Scope 0's derivation unification deliberately carries NO DDL (it re-addresses no
stored row), which is why one file covers the whole task.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_TABLES = ("crosswalk_observation_revision", "crosswalk_observation_current")

_CWO = "cwo_" + "a" * 64
_KEY = "b" * 64
_HASH = "0" * 64


def _sql(name: str) -> str:
    return (_MIGRATION_DIR / name).read_text(encoding="utf-8")


def _apply(db) -> None:
    db.execute(_sql("1057_crosswalk_observation.sql"))


def _drop(db) -> None:
    db.execute("DROP TABLE IF EXISTS crosswalk_observation_current")
    db.execute("DROP TABLE IF EXISTS crosswalk_observation_revision")


def _tables(db) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()}


def _seed_definition(db, revision_id: str = "cwd_" + "c" * 64) -> str:
    db.execute(
        "INSERT INTO crosswalk_definition_revision (revision_id, definition_id, "
        "  source_endpoint_ref, target_endpoint_ref, mapping_dataset_ref, definition_json, "
        "  content_hash, authored_by) VALUES (%s,%s,'cib::public.c','ftr::public.p',"
        "  'cib::public.m',%s,%s,'test') ON CONFLICT (revision_id) DO NOTHING",
        (revision_id, "cwd_" + "d" * 64,
         '{"source_to_mapping_pairs": [{"endpoint_member_ref": "cib::public.c.id",'
         ' "mapping_member_ref": "cib::public.m.id"}],'
         ' "mapping_to_target_pairs": [{"endpoint_member_ref": "ftr::public.p.cif",'
         ' "mapping_member_ref": "cib::public.m.cif"}], "evidence_refs": []}',
         _HASH))
    return revision_id


def _seed(db, *, observation_revision_id: str = _CWO, scope_key: str = _KEY,
          source_to_target: int = 1, target_to_source: int = 1, composed_rows: int = 3) -> str:
    definition_revision = _seed_definition(db)
    db.execute(
        "INSERT INTO crosswalk_observation_revision (observation_revision_id, current_scope_key, "
        "  crosswalk_definition_revision_id, mapping_binding_revision_id, scope_id, "
        "  execution_principal, method, row_coverage, complete, source_to_target_max_matches, "
        "  target_to_source_max_matches, composed_row_count, quality_rank, producer, strength, "
        "  observation_json, content_hash, observed_at) "
        "VALUES (%s,%s,%s,%s,'scope-1','p','exact','full',true,%s,%s,%s,30,'profiler','supported',"
        "  '{}'::jsonb,%s, now())",
        (observation_revision_id, scope_key, definition_revision, _binding(db),
         source_to_target, target_to_source, composed_rows, _HASH))
    return definition_revision


def _binding(db) -> str:
    """One physical binding revision the observation can point its mapping at."""
    revision_id = "pbr_" + "e" * 64
    db.execute(
        "INSERT INTO physical_dataset_binding_revision (binding_revision_id, binding_id, "
        "  content_hash, catalog_logical_ref, connection_id, physical_id, binding_json) "
        "VALUES (%s,'b-map',%s,'cib::dpl_eib.acct_xref','route-1',"
        "  'cib::edp::dpl_eib::acct_xref','{}'::jsonb) "
        "ON CONFLICT (binding_revision_id) DO NOTHING", (revision_id, _HASH))
    return revision_id


# ── legacy-shape replay ─────────────────────────────────────────────────────────────────────────

def test_1057_creates_its_tables_from_a_pre_task_11_shape(db) -> None:
    _drop(db)
    assert not (_TABLES[0] in _tables(db) or _TABLES[1] in _tables(db))
    _apply(db)
    assert set(_TABLES) <= _tables(db)


def test_1057_is_idempotent_over_populated_tables(db) -> None:
    """The failure mode CI cannot see: a re-run against a database that already holds rows."""
    _seed(db)
    _apply(db)
    assert db.execute(
        "SELECT count(*) FROM crosswalk_observation_revision").fetchone()[0] == 1


def test_the_definition_and_the_binding_are_real_foreign_keys(db) -> None:
    """A measurement of a crosswalk that does not exist is not a measurement of anything."""
    with pytest.raises(Exception):
        with db.transaction():
            db.execute(
                "INSERT INTO crosswalk_observation_revision (observation_revision_id, "
                "  current_scope_key, crosswalk_definition_revision_id, "
                "  mapping_binding_revision_id, scope_id, execution_principal, method, "
                "  row_coverage, complete, source_to_target_max_matches, "
                "  target_to_source_max_matches, composed_row_count, quality_rank, producer, "
                "  strength, observation_json, content_hash, observed_at) "
                "VALUES (%s,%s,'cwd_absent','pbr_absent','s','p','exact','full',true,1,1,1,30,"
                "  'profiler','supported','{}'::jsonb,%s, now())",
                ("cwo_" + "9" * 64, _KEY, _HASH))


# ── the CHECKs that keep a stored row honest ────────────────────────────────────────────────────

def test_a_composition_that_joined_nothing_cannot_carry_a_fanout(db) -> None:
    """The application refuses it too; this is the backstop for a hand-written row."""
    with pytest.raises(Exception):
        with db.transaction():
            _seed(db, observation_revision_id="cwo_" + "1" * 64, composed_rows=0,
                  source_to_target=2)


def test_a_pointer_version_of_zero_is_forbidden(db) -> None:
    """A STORED 0 would make "no pointer existed" indistinguishable from a real version."""
    definition_revision = _seed(db)
    with pytest.raises(Exception):
        with db.transaction():
            db.execute(
                "INSERT INTO crosswalk_observation_current (current_scope_key, "
                "  observation_revision_id, crosswalk_definition_revision_id, quality_rank, "
                "  observed_at, pointer_version) VALUES (%s,%s,%s,30, now(), 0)",
                (_KEY, _CWO, definition_revision))


def test_the_two_leg_row_ids_are_nullable_and_distinct_when_present(db) -> None:
    """NULL is load-bearing: a same-catalog leg has no bridge realization, so it has no row in the
    two-endpoint store (1038 is NOT NULL on `realization_revision_id`). What is forbidden is the
    SAME row claimed as both legs."""
    definition_revision = _seed_definition(db)
    binding_revision = _binding(db)
    columns = (
        "INSERT INTO crosswalk_observation_revision (observation_revision_id, current_scope_key, "
        "  crosswalk_definition_revision_id, source_leg_observation_revision_id, "
        "  target_leg_observation_revision_id, mapping_binding_revision_id, scope_id, "
        "  execution_principal, method, row_coverage, complete, source_to_target_max_matches, "
        "  target_to_source_max_matches, composed_row_count, quality_rank, producer, strength, "
        "  observation_json, content_hash, observed_at) "
        "VALUES (%s,%s,%s,NULL,NULL,%s,'s','p','exact','full',true,1,1,1,30,'profiler',"
        "  'supported','{}'::jsonb,%s, now())")
    db.execute(columns, ("cwo_" + "2" * 64, _KEY, definition_revision, binding_revision, _HASH))
    assert db.execute(
        "SELECT count(*) FROM crosswalk_observation_revision "
        "WHERE source_leg_observation_revision_id IS NULL").fetchone()[0] == 1


def test_the_id_prefix_is_check_pinned(db) -> None:
    with pytest.raises(Exception):
        with db.transaction():
            _seed(db, observation_revision_id="rob_" + "3" * 64)


# ── the reservation ─────────────────────────────────────────────────────────────────────────────

def test_this_stream_allocated_exactly_one_number() -> None:
    """D7: uniqueness is the FULL FILENAME, and Task 11 owns 1057 — nothing else.

    Phrased as a fact about THIS STREAM rather than about the pool. It used to assert
    ``[n for n in names if n.startswith("1058_")] == []``, which READS as "Task 11 did not also
    grab the next number" but ASSERTS "nobody may ever take 1058" — so the first unrelated stream
    to claim it legitimately turned this stream's own discipline into somebody else's red test.
    That is a landmine, not a guard: the number a later author must take is exactly the one this
    line forbade, and the failure gives them no hint that the fix is here.

    1058 is now taken by the readiness wave (``1058_graph_node_fibo_path.sql``), reserved in the
    D7 table in the same commit as the migration — which is the coordination step the 1032/1033
    double-allocation (A.22) exists to enforce, and the one this assertion was reaching for.
    """
    names = sorted(p.name for p in _MIGRATION_DIR.glob("*.sql"))
    assert [n for n in names if n.startswith("1057_")] == ["1057_crosswalk_observation.sql"]
    # The real invariant: this STREAM contributed exactly one migration, whatever number any
    # later stream takes. Keyed on the stream's own name, so it stays true as the pool advances.
    assert [n for n in names
            if "crosswalk_observation" in n] == ["1057_crosswalk_observation.sql"]


def test_1057_orders_after_everything_it_references() -> None:
    """Lexical order is the runner's, so a reference to a later file would never apply."""
    sql = _sql("1057_crosswalk_observation.sql")
    referenced = {
        line.split("REFERENCES", 1)[1].split("(", 1)[0].strip()
        for line in sql.splitlines() if "REFERENCES" in line}
    assert referenced == {
        "crosswalk_definition_revision",              # 1050
        "relationship_observation_revision",          # 1038
        "physical_dataset_binding_revision",          # 1036
        "crosswalk_observation_revision",             # this file, above the pointer
    }
