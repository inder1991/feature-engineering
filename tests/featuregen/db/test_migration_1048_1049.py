"""1048/1049 — the Release-B serving and temporal policy stores.

Same discipline as `test_migration_1051`/`_1052`: `apply_migrations` always runs on a FRESH
database in CI, so a migration that trips over an existing shape passes every test and fails on the
one database that matters ("migration audits are blind to legacy data"). These tests drop back to a
PRE-1048/1049 shape, re-apply the SQL exactly as the runner does, and then re-apply it AGAIN over
populated tables.

They also pin the D7 reservation reality on the tree as it IS: 1045/1046/1047/1051/1052 exist,
while 1050 (Release C crosswalk) and the 1053-1055 Phase-G RESERVED
BLOCK do not — 1048/1049 may therefore not depend on anything those numbers would create. The
1053-1055 assertion is deliberately an ABSENCE check: that block belongs to a parallel session, so
this stream must neither create those files nor assume they exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations
from featuregen.overlay.upload.profile_vocab import TemporalStorageModel
from featuregen.overlay.upload.source_selection import DatasetNeedRole, ServingPurpose
from featuregen.overlay.upload.temporal_policy import TemporalSelectionKind

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_TABLES = ("dataset_serving_policy_revision", "dataset_serving_policy_current",
           "dataset_temporal_policy_revision", "dataset_temporal_policy_current")

_DSP = "dsp_" + "0" * 64
_DTP = "dtp_" + "0" * 64
_HASH = "0" * 64


def _sql(name: str) -> str:
    return (_MIGRATION_DIR / name).read_text(encoding="utf-8")


def _apply(db) -> None:
    db.execute(_sql("1048_dataset_serving_policy.sql"))
    db.execute(_sql("1049_dataset_temporal_policy.sql"))


def _tables(db) -> set[str]:
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _drop(db) -> None:
    # Pointers first: they carry the FK onto the revision tables.
    for table in ("dataset_serving_policy_current", "dataset_temporal_policy_current",
                  "dataset_serving_policy_revision", "dataset_temporal_policy_revision"):
        db.execute(f"DROP TABLE IF EXISTS {table}")


def _seed_serving(db, revision_id: str = _DSP) -> None:
    db.execute(
        "INSERT INTO dataset_serving_policy_revision (revision_id, entity_id, need_role, "
        "  serving_purpose, eligible_dataset_refs, preferred_dataset_refs, content_hash, "
        "  authored_by) "
        "VALUES (%s,'customer','population','analytical','[\"bank::public.cust\"]',"
        "        '[\"bank::public.cust\"]',%s,'user:priya')",
        (revision_id, _HASH))
    db.execute(
        "INSERT INTO dataset_serving_policy_current (entity_id, need_role, serving_purpose, "
        "  revision_id, pointer_version, declared_by) "
        "VALUES ('customer','population','analytical',%s,1,'user:priya')", (revision_id,))


def _seed_temporal(db, revision_id: str = _DTP) -> None:
    db.execute(
        "INSERT INTO dataset_temporal_policy_revision (revision_id, dataset_logical_ref, "
        "  temporal_storage_model, current_selection, historical_selection, content_hash, "
        "  authored_by) "
        "VALUES (%s,'bank::public.seg','scd2','current_record','valid_at_report_cutoff',%s,'u')",
        (revision_id, _HASH))
    db.execute(
        "INSERT INTO dataset_temporal_policy_current (dataset_logical_ref, revision_id, "
        "  pointer_version, declared_by) VALUES ('bank::public.seg',%s,1,'u')", (revision_id,))


# ── apply / re-apply ────────────────────────────────────────────────────────────────────────────

def test_1048_1049_create_their_tables_from_a_pre_release_b_shape(db) -> None:
    _drop(db)
    assert not (_tables(db) & set(_TABLES))
    _apply(db)
    assert set(_TABLES) <= _tables(db)


def test_1048_1049_are_re_runnable_over_POPULATED_tables(db) -> None:
    """The runner ledgers by name+checksum, but a re-applied file on a repaired or hand-applied
    database must still be a no-op — and must not touch rows that are already there."""
    _seed_serving(db)
    _seed_temporal(db)
    _apply(db)
    _apply(db)
    assert db.execute(
        "SELECT count(*) FROM dataset_serving_policy_revision").fetchone()[0] == 1
    assert db.execute(
        "SELECT count(*) FROM dataset_temporal_policy_current").fetchone()[0] == 1


# ── the CHECK vocabularies are the code's vocabularies ──────────────────────────────────────────

def test_1048_need_role_and_serving_purpose_checks_admit_every_member(db) -> None:
    """Derived FROM the enums, so adding a member without migrating is what fails here — not a
    hand-typed list quietly drifting from the contract."""
    for role in DatasetNeedRole:
        for purpose in ServingPurpose:
            db.execute(
                "INSERT INTO dataset_serving_policy_revision (revision_id, entity_id, need_role, "
                "  serving_purpose, eligible_dataset_refs, content_hash, authored_by) "
                "VALUES (%s,'customer',%s,%s,'[\"bank::public.cust\"]',%s,'u')",
                ("dsp_" + f"{role.value}{purpose.value}".encode().hex().ljust(64, "0")[:64],
                 role.value, purpose.value, _HASH))
    stored = {tuple(r) for r in db.execute(
        "SELECT need_role, serving_purpose FROM dataset_serving_policy_revision").fetchall()}
    assert stored == {(r.value, p.value) for r in DatasetNeedRole for p in ServingPurpose}


@pytest.mark.parametrize("column,value", [
    ("need_role", "whatever"),
    ("serving_purpose", "operational"),   # a superseded-plan purpose (D12.9) — dropped, not stored
])
def test_1048_refuses_an_off_vocabulary_value(db, column, value) -> None:
    role, purpose = ("population", "analytical")
    if column == "need_role":
        role = value
    else:
        purpose = value
    with pytest.raises(Exception):   # noqa: B017 — psycopg CheckViolation, driver-typed
        db.execute(
            "INSERT INTO dataset_serving_policy_revision (revision_id, entity_id, need_role, "
            "  serving_purpose, eligible_dataset_refs, content_hash, authored_by) "
            "VALUES (%s,'customer',%s,%s,'[\"bank::public.cust\"]',%s,'u')",
            (_DSP, role, purpose, _HASH))


def test_1048_refuses_a_policy_with_no_eligible_dataset(db) -> None:
    """A policy that can never select is not a policy — the CHECK mirrors the contract's refusal."""
    with pytest.raises(Exception):   # noqa: B017
        db.execute(
            "INSERT INTO dataset_serving_policy_revision (revision_id, entity_id, need_role, "
            "  serving_purpose, eligible_dataset_refs, content_hash, authored_by) "
            "VALUES (%s,'customer','population','analytical','[]',%s,'u')", (_DSP, _HASH))


def test_1049_checks_admit_every_storage_model_and_selection_kind(db) -> None:
    for i, model in enumerate(TemporalStorageModel):
        for j, kind in enumerate(TemporalSelectionKind):
            db.execute(
                "INSERT INTO dataset_temporal_policy_revision (revision_id, dataset_logical_ref, "
                "  temporal_storage_model, current_selection, historical_selection, content_hash, "
                "  authored_by) VALUES (%s,'bank::public.seg',%s,%s,%s,%s,'u')",
                ("dtp_" + f"{i}{j}".rjust(64, "0"), model.value, kind.value, kind.value, _HASH))
    stored = {r[0] for r in db.execute(
        "SELECT DISTINCT temporal_storage_model FROM dataset_temporal_policy_revision").fetchall()}
    assert stored == {m.value for m in TemporalStorageModel}
    kinds = {r[0] for r in db.execute(
        "SELECT DISTINCT current_selection FROM dataset_temporal_policy_revision").fetchall()}
    assert kinds == {k.value for k in TemporalSelectionKind}


# ── pointers cannot outlive their revisions ─────────────────────────────────────────────────────

@pytest.mark.parametrize("table,sql", [
    ("dataset_serving_policy_current",
     "INSERT INTO dataset_serving_policy_current (entity_id, need_role, serving_purpose, "
     "revision_id, pointer_version, declared_by) "
     "VALUES ('customer','population','analytical','dsp_ghost',1,'u')"),
    ("dataset_temporal_policy_current",
     "INSERT INTO dataset_temporal_policy_current (dataset_logical_ref, revision_id, "
     "pointer_version, declared_by) VALUES ('bank::public.seg','dtp_ghost',1,'u')"),
])
def test_a_current_pointer_must_reference_a_real_revision(db, table, sql) -> None:
    with pytest.raises(Exception):   # noqa: B017 — ForeignKeyViolation, driver-typed
        db.execute(sql)


@pytest.mark.parametrize("table,column", [
    ("dataset_serving_policy_current", "pointer_version"),
    ("dataset_temporal_policy_current", "pointer_version"),
])
def test_a_current_pointer_version_starts_at_one(db, table, column) -> None:
    """`expected_pointer_version=0` is the CLAIM that no policy existed; a STORED 0 would make the
    first-write claim indistinguishable from a real version."""
    _seed_serving(db)
    _seed_temporal(db)
    with pytest.raises(Exception):   # noqa: B017 — CheckViolation
        db.execute(f"UPDATE {table} SET {column} = 0")


# ── D7 reservation reality ──────────────────────────────────────────────────────────────────────

def test_1048_1049_apply_with_their_real_neighbours_and_none_of_the_unwritten_reservations() -> None:
    names = {p.name for p in _MIGRATION_DIR.glob("*.sql")}
    present = {n.split("_", 1)[0] for n in names}
    assert {"1045", "1046", "1047", "1048", "1049", "1051", "1052"} <= present
    # 1044 arrived with the Track-2 merge; 1050 is Release C's crosswalk store, and 1053-1055
    # are the Phase-G RESERVED BLOCK owned by a PARALLEL session. None is on this tree; asserting
    # their ABSENCE is what keeps this stream from silently claiming a number it does not own.
    assert not ({"1050", "1053", "1054", "1055"} & present)


def test_this_stream_allocated_exactly_two_numbers() -> None:
    """D7: uniqueness is the FULL FILENAME, and this task owns 1048 and 1049 — nothing else. A
    second file at either number, or a third file from this stream, is an unrecorded allocation."""
    names = sorted(p.name for p in _MIGRATION_DIR.glob("*.sql"))
    assert [n for n in names if n.startswith("1048_")] == ["1048_dataset_serving_policy.sql"]
    assert [n for n in names if n.startswith("1049_")] == ["1049_dataset_temporal_policy.sql"]
