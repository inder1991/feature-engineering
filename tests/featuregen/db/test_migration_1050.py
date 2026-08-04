"""1050 — the Release C crosswalk definition store.

Same discipline as `test_migration_1048_1049`: `apply_migrations` always runs on a FRESH database in
CI, so a migration that trips over an existing shape passes every test and fails on the one database
that matters ("migration audits are blind to legacy data"). These tests drop back to a pre-1050
shape, re-apply the SQL exactly as the runner does, then re-apply it AGAIN over populated tables.

They also pin the D7 reservation reality — with ONE deliberate difference from 1048/1049's version.
The 1053-1055 Phase-G RESERVED BLOCK belongs to a PARALLEL session that may land while this branch
is open, so this file asserts NOTHING about whether those files exist. What it does assert is that
this stream allocated 1050 and only 1050: tolerance about a neighbour's numbers is not licence to
be vague about your own.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations
from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_TABLES = ("crosswalk_definition_revision", "crosswalk_definition_current")

_CWD_A = "cwd_" + "a" * 64
_CWD_R = "cwd_" + "b" * 64
_HASH = "0" * 64
_JSON = ('{"source_to_mapping_pairs": [{"endpoint_member_ref": "cib::public.c.id",'
         ' "mapping_member_ref": "cib::public.m.id"}],'
         ' "mapping_to_target_pairs": [{"endpoint_member_ref": "ftr::public.p.cif",'
         ' "mapping_member_ref": "cib::public.m.cif"}], "evidence_refs": []}')


def _sql(name: str) -> str:
    return (_MIGRATION_DIR / name).read_text(encoding="utf-8")


def _apply(db) -> None:
    db.execute(_sql("1050_crosswalk_definition.sql"))


def _tables(db) -> set[str]:
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    return {r[0] for r in rows}


def _drop(db) -> None:
    # The pointer carries the FK onto the revision table, so it goes first.
    db.execute("DROP TABLE IF EXISTS crosswalk_definition_current")
    db.execute("DROP TABLE IF EXISTS crosswalk_definition_revision")


def _seed(db, revision_id: str = _CWD_R, *, definition_id: str = _CWD_A,
          source: str = "cib::public.c", target: str = "ftr::public.p",
          mapping: str = "cib::public.m", json_payload: str = _JSON) -> None:
    db.execute(
        "INSERT INTO crosswalk_definition_revision (revision_id, definition_id, "
        "  source_endpoint_ref, target_endpoint_ref, mapping_dataset_ref, definition_json, "
        "  content_hash, authored_by) VALUES (%s,%s,%s,%s,%s,%s,%s,'user:priya')",
        (revision_id, definition_id, source, target, mapping, json_payload, _HASH))


def _seed_pointer(db, revision_id: str = _CWD_R, definition_id: str = _CWD_A) -> None:
    db.execute(
        "INSERT INTO crosswalk_definition_current (definition_id, revision_id, review_status, "
        "  pointer_version, declared_by) VALUES (%s,%s,'unreviewed',1,'user:priya')",
        (definition_id, revision_id))


# ── apply / re-apply ────────────────────────────────────────────────────────────────────────────

def test_1050_creates_its_tables_from_a_pre_release_c_shape(db) -> None:
    _drop(db)
    assert not (_tables(db) & set(_TABLES))
    _apply(db)
    assert set(_TABLES) <= _tables(db)


def test_1050_is_re_runnable_over_POPULATED_tables(db) -> None:
    """The runner ledgers by name+checksum, but a re-applied file on a repaired or hand-applied
    database must still be a no-op — and must not touch rows already there."""
    _seed(db)
    _seed_pointer(db)
    _apply(db)
    _apply(db)
    assert db.execute("SELECT count(*) FROM crosswalk_definition_revision").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM crosswalk_definition_current").fetchone()[0] == 1


# ── the CHECKs are the contract's rules ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("column,value", [
    ("revision_id", "cwr_" + "b" * 64),      # wrong family prefix
    ("revision_id", "cwd_NOTHEX" + "b" * 58),
    ("definition_id", "dtp_" + "a" * 64),    # a temporal policy id in a crosswalk column
])
def test_1050_refuses_an_off_family_identity(db, column, value) -> None:
    kwargs = {"revision_id": _CWD_R, "definition_id": _CWD_A}
    kwargs[column] = value
    with pytest.raises(Exception):   # noqa: B017 — psycopg CheckViolation, driver-typed
        _seed(db, kwargs["revision_id"], definition_id=kwargs["definition_id"])


def test_1050_refuses_a_crosswalk_between_one_endpoint_and_itself(db) -> None:
    with pytest.raises(Exception):   # noqa: B017
        _seed(db, source="cib::public.c", target="cib::public.c")


def test_1050_refuses_a_mapping_dataset_that_is_also_an_endpoint(db) -> None:
    """That shape is a DIRECT relationship, which belongs to the bridge family."""
    with pytest.raises(Exception):   # noqa: B017
        _seed(db, mapping="cib::public.c")


def test_1050_refuses_a_leg_with_no_pairs(db) -> None:
    empty = ('{"source_to_mapping_pairs": [], "mapping_to_target_pairs": ['
             '{"endpoint_member_ref": "ftr::public.p.cif", "mapping_member_ref":'
             ' "cib::public.m.cif"}], "evidence_refs": []}')
    with pytest.raises(Exception):   # noqa: B017
        _seed(db, json_payload=empty)


def test_1050_refuses_an_unbounded_leg(db) -> None:
    pairs = ", ".join(
        f'{{"endpoint_member_ref": "cib::public.c.k{i}", "mapping_member_ref": "cib::public.m.k{i}"}}'
        for i in range(17))
    wide = ('{"source_to_mapping_pairs": [' + pairs + '], "mapping_to_target_pairs": ['
            '{"endpoint_member_ref": "ftr::public.p.cif", "mapping_member_ref":'
            ' "cib::public.m.cif"}], "evidence_refs": []}')
    with pytest.raises(Exception):   # noqa: B017
        _seed(db, json_payload=wide)


def test_1050_review_status_check_admits_exactly_the_link_review_vocabulary(db) -> None:
    """No third review vocabulary: the CHECK is derived FROM `LinkReviewStatus`, so adding a member
    without migrating fails here rather than drifting quietly."""
    _seed(db)
    _seed_pointer(db)
    for status in LinkReviewStatus:
        db.execute("UPDATE crosswalk_definition_current SET review_status = %s", (status.value,))
    stored = db.execute("SELECT review_status FROM crosswalk_definition_current").fetchone()[0]
    assert stored in {s.value for s in LinkReviewStatus}
    with pytest.raises(Exception):   # noqa: B017
        db.execute("UPDATE crosswalk_definition_current SET review_status = 'approved'")


def test_a_current_pointer_must_reference_a_real_revision(db) -> None:
    with pytest.raises(Exception):   # noqa: B017 — ForeignKeyViolation, driver-typed
        _seed_pointer(db, revision_id="cwd_" + "f" * 64)


def test_a_current_pointer_version_starts_at_one(db) -> None:
    """`expected_pointer_version=0` is the CLAIM that no crosswalk existed; a STORED 0 would make
    the first-write claim indistinguishable from a real version."""
    _seed(db)
    _seed_pointer(db)
    with pytest.raises(Exception):   # noqa: B017 — CheckViolation
        db.execute("UPDATE crosswalk_definition_current SET pointer_version = 0")


def test_1050_stores_no_safety_availability_or_execution_column(db) -> None:
    """Discovery and execution safety are SEPARATE axes (§5.8). A nullable safety column would be
    read as an answer, so there is none — and this test is why nobody adds one by reflex."""
    columns = {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name IN ('crosswalk_definition_revision','crosswalk_definition_current')"
    ).fetchall()}
    assert not (columns & {"safety_status", "availability", "execution_revision_id",
                           "execution_tier", "sandbox_eligible", "production_eligible"})


# ── D7 reservation reality ──────────────────────────────────────────────────────────────────────

def test_1050_applies_with_its_real_neighbours() -> None:
    names = {p.name for p in _MIGRATION_DIR.glob("*.sql")}
    present = {n.split("_", 1)[0] for n in names}
    assert {"1045", "1046", "1047", "1048", "1049", "1050", "1051", "1052"} <= present


def test_the_phase_g_block_may_be_present_or_absent() -> None:
    """1053-1055 belong to a PARALLEL session. This branch must work whether or not their merge has
    landed, so the assertion is that their presence is IRRELEVANT here — never that they are
    missing, which would break the moment Phase G lands, and never that they exist, which would
    break today."""
    names = {p.name for p in _MIGRATION_DIR.glob("*.sql")}
    present = {n.split("_", 1)[0] for n in names}
    assert {"1050"} <= present
    # The property that MAKES their presence irrelevant, asserted directly rather than assumed:
    # 1050 references nothing outside its own two tables, so no ordering against 1053-1055 exists.
    sql = _sql("1050_crosswalk_definition.sql")
    referenced = {
        line.split("REFERENCES", 1)[1].split("(", 1)[0].strip()
        for line in sql.splitlines() if "REFERENCES" in line}
    assert referenced <= {"crosswalk_definition_revision"}


def test_this_stream_allocated_exactly_one_number() -> None:
    """D7: uniqueness is the FULL FILENAME, and this task owns 1050 — nothing else. A second file at
    1050, or any 1053+ file from this stream, is an unrecorded allocation."""
    names = sorted(p.name for p in _MIGRATION_DIR.glob("*.sql"))
    assert [n for n in names if n.startswith("1050_")] == ["1050_crosswalk_definition.sql"]
