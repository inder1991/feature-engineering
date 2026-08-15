"""1055 — G-3's active-revision pointer, audited against a POPULATED legacy shape.

The standing lesson these tests exist for: `apply_migrations` always runs on a FRESH database in CI,
so a migration that trips over an existing shape passes every test and fails on the one database
that matters. So each test here drops back to a pre-1055 world, applies the SQL exactly as the
runner does, POPULATES the table, and applies it AGAIN — which is the case a re-deploy actually
performs and the case a `DROP`/`ALTER` in a migration destroys.

**The migration is a FILE.** Nothing here or anywhere else applies it to a cluster; the test
database is the only thing that has ever executed it.

NEIGHBOUR TOLERANCE (the 1050 rule): nothing asserts anything about 1053/1054/1056+, which belong to
other streams. What it does assert is that this stream allocated 1055 and only 1055.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_SQL = "1055_feature_active_revision.sql"
_GROUP = "cif_daily"


def _apply(db) -> None:
    db.execute((_MIGRATION_DIR / _SQL).read_text(encoding="utf-8"))


def _drop(db) -> None:
    db.execute("DROP TABLE IF EXISTS feature_active_revision")


def _seed_generation(db, generation_id: str = "gen_1055_a") -> str:
    """A real generation row, because 1055's FK is real: a pointer naming a generation the plane
    never recorded would claim a publication of bytes nobody compiled."""
    db.execute(
        "INSERT INTO materialization_generation (generation_id, logical_group_name, "
        "materialization_contract_hash, group_plan_hash, generated_project_hash, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (generation_id, _GROUP, "c" * 64, "p" * 64, "g" * 64, "2026-08-15T10:00:00+00:00"))
    return generation_id


def _insert(db, *, revision_id="frev_1", generation_id="gen_1055_a", seq=0,
            group_name=_GROUP, mechanism="VERSIONED_POINTER") -> None:
    db.execute(
        "INSERT INTO feature_active_revision (revision_id, logical_group_name, generation_id, "
        "run_id, published_object, capability_attestation_id, publication_mechanism, seq, "
        "activated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (revision_id, group_name, generation_id, "mrun_1", f"sandbox_feature.{group_name}",
         "probe-1", mechanism, seq, "2026-08-15T10:00:00+00:00"))


@pytest.fixture
def fresh(db):
    """A pre-1055 world, re-migrated. The table is dropped rather than assumed absent, because the
    session's conftest has already applied every migration."""
    _drop(db)
    _apply(db)
    _seed_generation(db)
    return db


# ── the audit: re-application over a POPULATED table ─────────────────────────────────────────────


def test_re_applying_over_a_populated_table_destroys_nothing(fresh) -> None:
    """The case CI never runs. A `DROP TABLE`/`CREATE`, or an `ALTER` that assumed an empty table,
    would pass on a fresh database and silently erase every pointer on a deployed one — and this is
    the only record that says a feature was ever readable."""
    _insert(fresh)

    _apply(fresh)

    rows = fresh.execute(
        "SELECT revision_id, seq FROM feature_active_revision ORDER BY seq").fetchall()
    assert rows == [("frev_1", 0)]


def test_the_migration_is_re_runnable_from_nothing(db) -> None:
    """Twice from a clean slate — the ordinary apply path, and the one a partial deploy repeats."""
    _drop(db)
    _apply(db)
    _apply(db)

    assert db.execute(
        "SELECT to_regclass('public.feature_active_revision')").fetchone()[0] is not None


def test_this_stream_allocated_1055_and_only_1055() -> None:
    """Tolerance about a neighbour's numbers is not licence to be vague about your own. 1055 has
    been reserved for this table since Phase G's T1 (1053's own header says so), and 1056-1068 are
    taken by later streams — so it could not simply be re-allocated."""
    assert (_MIGRATION_DIR / _SQL).is_file()
    mine = sorted(p.name for p in _MIGRATION_DIR.glob("1055*"))
    assert mine == [_SQL]


# ── the ordering guard: 1044's rule, applied per GROUP ───────────────────────────────────────────


def test_a_pointer_that_does_not_extend_its_group_is_refused(fresh) -> None:
    """A stale publisher writing after a newer one, or two writers for one swap. Either leaves the
    table unable to say what a reader sees, and the append-only guards make that unrepairable — so
    the database refuses the write rather than the read."""
    _insert(fresh, revision_id="frev_1", seq=0)
    _seed_generation(fresh, "gen_1055_b")

    with pytest.raises(psycopg.errors.RaiseException, match="does not extend group"), \
            fresh.transaction():
        _insert(fresh, revision_id="frev_stale", generation_id="gen_1055_b", seq=0)

    _insert(fresh, revision_id="frev_2", generation_id="gen_1055_b", seq=1)
    assert fresh.execute("SELECT count(*) FROM feature_active_revision").fetchone()[0] == 2


def test_the_order_is_per_GROUP_not_global(fresh) -> None:
    """Publication is atomic per group (§10.1), so two groups publish independently and a global
    sequence would make one group's swap refuse because another had published."""
    _insert(fresh, revision_id="frev_a", seq=0, group_name=_GROUP)

    _insert(fresh, revision_id="frev_b", seq=0, group_name="merchant_daily")

    assert fresh.execute(
        "SELECT count(*) FROM feature_active_revision WHERE seq = 0").fetchone()[0] == 2


# ── the append-only guards: 1034's function, not a copy ──────────────────────────────────────────


@pytest.mark.parametrize("statement", [
    "UPDATE feature_active_revision SET published_object = 'other'",
    "DELETE FROM feature_active_revision",
    "TRUNCATE feature_active_revision",
])
def test_a_recorded_pointer_cannot_be_rewritten(fresh, statement) -> None:
    """1034's rule: a record that can be rewritten proves nothing — and it applies with more force
    here than anywhere else in the plane, because this is the only record that says a feature was
    ever readable. TRUNCATE needs its own STATEMENT-level trigger; a FOR EACH ROW one never fires."""
    _insert(fresh)

    with pytest.raises(psycopg.errors.RaiseException), fresh.transaction():
        fresh.execute(statement)

    assert fresh.execute("SELECT count(*) FROM feature_active_revision").fetchone()[0] == 1


def test_the_append_only_trigger_is_1034s_OWN_function_not_a_copy(fresh) -> None:
    """One rule, one message, one place to change. A private copy would be a second place for it to
    drift and a second message for an operator to meet — the same assertion 1054 makes."""
    mine, theirs = (fresh.execute(
        "SELECT tgfoid FROM pg_trigger WHERE tgname = %s", (name,)).fetchone()
        for name in ("feature_active_revision_no_mutation",
                     "materialization_generation_no_mutation"))

    assert mine == theirs


def test_a_pointer_naming_no_generation_is_refused(fresh) -> None:
    """The FK is real. A pointer naming a generation the plane never recorded would claim a
    publication of bytes nobody compiled, and the append-only table could never take it back."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation), fresh.transaction():
        _insert(fresh, revision_id="frev_orphan", generation_id="gen_nobody_compiled")


def test_the_mechanism_vocabulary_is_CLOSED(fresh) -> None:
    """§10 rejects INSERT OVERWRITE outright, and `PublishMechanism` has no member for it. The CHECK
    is what stops a writer inventing one that no probe could ever have been pointed at."""
    with pytest.raises(psycopg.errors.CheckViolation), fresh.transaction():
        _insert(fresh, revision_id="frev_bad", mechanism="INSERT_OVERWRITE")
