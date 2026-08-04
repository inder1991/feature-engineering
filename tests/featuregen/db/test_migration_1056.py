"""1056 — the `pii_use_policy` revision + CAS pointer store (D14).

Same discipline as `test_migration_1052`: `apply_migrations` always runs on a FRESH database in CI,
so a store that trips over pre-existing rows passes every test and fails on the one database that
matters ("migration audits are blind to legacy data"). These tests DROP the two tables, seed the
legacy shape a pre-1056 database has (which is: nothing, plus catalog rows that must survive), then
re-apply the migration SQL exactly as the runner does.

They also pin the D7 reservation reality as THIS stream's own claim: 1056 is the only number this
slice allocated. 1053-1055 belong to a PARALLEL session and are deliberately not asserted either
way — this store depends on nothing they create, and a blanket "nothing exists above 1052" would
fail the moment that session merges, for a stream that neither owns those numbers nor depends on
them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import featuregen.db.migrations as _migrations

_MIGRATION_DIR = Path(_migrations.__file__).resolve().parent / "migrations"
_TABLES = ("pii_use_policy_revision", "pii_use_policy_current")
#: Every migration filename THIS stream allocated. The full name, not the number: the ledger keys
#: on name+checksum, and two streams reaching for 1056 with different filenames is exactly the
#: collision the pin below has to see.
_STREAM_MIGRATIONS = ("1056_pii_use_policy.sql",)

_HASH = "a" * 64
_REV = f"pup_{_HASH}"
#: The legacy-replay seeds write rows by HAND, so they supply an attestation of the right SHAPE
#: rather than a real one — these tests are about the DDL (columns, CHECKs, keys), and the store's
#: own suite is where a real attestation is computed and verified.
_ATTEST = "b" * 64


def _migration_1056_sql() -> str:
    return (_MIGRATION_DIR / "1056_pii_use_policy.sql").read_text(encoding="utf-8")


def _tables(db) -> set[str]:
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = ANY(%s)",
        (list(_TABLES),)).fetchall()
    return {r[0] for r in rows}


def _drop(db) -> None:
    db.execute("DROP TABLE IF EXISTS pii_use_policy_current")
    db.execute("DROP TABLE IF EXISTS pii_use_policy_revision")


def _seed_legacy_catalog(db) -> None:
    """One graph_node row written the way a PRE-1056 ingest wrote it — a pii-classed column with no
    knowledge that a data-use policy could ever exist. It must survive untouched."""
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "                        data_type, concept) "
        "VALUES ('bank', 'public.cust.pep_ind', 'column', 'cust', 'pep_ind', 'text', 'pep_flag')")


def _approve(db, *, concept="pep_flag", purpose="AML transaction monitoring", status="active",
             revision_id=_REV, content_hash=_HASH, approved_by="admin@bank",
             attestation_hash=_ATTEST):
    db.execute(
        "INSERT INTO pii_use_policy_revision (revision_id, concept_name, purpose, status, "
        "content_hash, approved_by, attestation_hash) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (revision_id, concept, purpose, status, content_hash, approved_by, attestation_hash))


def _point(db, *, concept="pep_flag", revision_id=_REV, pointer_version=1, declared_by="admin@bank",
           attestation_hash=_ATTEST):
    db.execute(
        "INSERT INTO pii_use_policy_current (concept_name, revision_id, pointer_version, "
        "declared_by, attestation_hash) VALUES (%s,%s,%s,%s,%s)",
        (concept, revision_id, pointer_version, declared_by, attestation_hash))


def test_1056_creates_the_store_over_a_legacy_catalog(db) -> None:
    _drop(db)
    _seed_legacy_catalog(db)
    assert not _tables(db)

    db.execute(_migration_1056_sql())

    assert _tables(db) == set(_TABLES)
    # The legacy row SURVIVES untouched: adding a policy store must not touch the catalog it
    # governs, and in particular must not fabricate a policy for a concept nobody declared.
    assert db.execute(
        "SELECT concept FROM graph_node WHERE object_ref = 'public.cust.pep_ind'"
    ).fetchone() == ("pep_flag",)
    assert db.execute("SELECT count(*) FROM pii_use_policy_current").fetchone()[0] == 0


def test_1056_is_re_runnable_against_an_already_migrated_database(db) -> None:
    """The runner ledgers by name+checksum, but a re-applied file must still be a no-op — the
    `IF NOT EXISTS` guards are what make a repaired or hand-applied database safe."""
    _drop(db)
    db.execute(_migration_1056_sql())
    _approve(db)
    _point(db)

    db.execute(_migration_1056_sql())

    assert _tables(db) == set(_TABLES)
    assert db.execute("SELECT count(*) FROM pii_use_policy_revision").fetchone()[0] == 1
    assert db.execute(
        "SELECT pointer_version FROM pii_use_policy_current").fetchone() == (1,)


def test_1056_pins_the_revision_id_prefix(db) -> None:
    """`pup_` + a 64-hex content hash, CHECK-pinned like `cpr_`/`pbr_`/`dtp_`: a revision id that is
    not derived from the content it names cannot be inserted at all."""
    with pytest.raises(Exception):   # noqa: B017 — psycopg CheckViolation, driver-typed
        _approve(db, revision_id="dtp_" + _HASH)
    with pytest.raises(Exception):   # noqa: B017
        _approve(db, revision_id="pup_not-a-hash")


def test_1056_status_is_a_closed_two_value_vocabulary(db) -> None:
    """`active` | `revoked` and nothing else — a third value would be a state the gate has no
    reading for, and the gate's only safe reading of an unknown state is to refuse."""
    for i, status in enumerate(("active", "revoked")):
        _approve(db, status=status, revision_id=f"pup_{str(i) * 64}", content_hash=str(i) * 64)
    with pytest.raises(Exception):   # noqa: B017
        _approve(db, status="pending", revision_id="pup_" + "b" * 64, content_hash="b" * 64)


def test_1056_bounds_the_purpose_text_in_the_database_too(db) -> None:
    """The bound is pinned in BOTH places that can enforce it — `pii_policy.normalize_purpose` and
    this CHECK. The python bound is the one that produces a readable refusal; this one is the one
    that still holds when a row arrives by any other route."""
    with pytest.raises(Exception):   # noqa: B017
        _approve(db, purpose="AML", revision_id="pup_" + "c" * 64, content_hash="c" * 64)
    with pytest.raises(Exception):   # noqa: B017
        _approve(db, purpose="x" * 301, revision_id="pup_" + "d" * 64, content_hash="d" * 64)


def test_1056_pointer_cannot_reference_a_revision_that_does_not_exist(db) -> None:
    """The FK is what makes "the pointer names an immutable revision" true rather than hoped for."""
    with pytest.raises(Exception):   # noqa: B017
        _point(db, revision_id="pup_" + "e" * 64)


def test_1056_the_pointer_FK_IS_COMPOSITE_so_it_cannot_cross_concepts(db) -> None:
    """The review's F1, refused by the DATABASE rather than only by the code that reads it.

    A single-column `REFERENCES pii_use_policy_revision(revision_id)` is satisfied by ANY revision,
    including one declared for a different concept — and a pointer for a REVOKED concept aimed at
    another concept's ACTIVE revision is exactly how one approval came to license a meaning nobody
    approved. The composite key is the layer no later "simplification" of the store or the reader
    can undo."""
    _approve(db, concept="geolocation", revision_id="pup_" + "1" * 64, content_hash="1" * 64)
    # its own concept: fine
    _point(db, concept="geolocation", revision_id="pup_" + "1" * 64)
    # another concept's revision: refused, and the pointer table stays as it was
    with pytest.raises(Exception):   # noqa: B017
        _point(db, concept="pep_flag", revision_id="pup_" + "1" * 64)


def test_1056_seals_the_approver_and_the_declarer_with_an_attestation_column(db) -> None:
    """F3's shape, pinned as DDL. `approved_by` / `declared_by` are outside content identity (which
    is correct — re-approval must resolve to the original revision id), so each row carries a second
    hash over its own provenance. NOT NULL and hex-shaped: a nullable one would have to be read as
    "unverifiable", which is the state the column exists to make impossible."""
    revision_cols = {r[0]: r[1] for r in db.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'pii_use_policy_revision'").fetchall()}
    pointer_cols = {r[0]: r[1] for r in db.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'pii_use_policy_current'").fetchall()}
    assert revision_cols.get("attestation_hash") == "NO"
    assert pointer_cols.get("attestation_hash") == "NO"
    with pytest.raises(Exception):   # noqa: B017
        _approve(db, revision_id="pup_" + "2" * 64, content_hash="2" * 64,
                 attestation_hash="not-a-hash")


def test_1056_carries_the_contract_side_of_the_same_surface(db) -> None:
    """F2: the covering policy revisions ride the CONTRACT, not just the in-memory idea. Additive,
    defaulting to the empty array — which means "this feature bound no personal data", the state
    every pre-1056 contract is honestly in."""
    row = db.execute(
        "SELECT data_type, column_default, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'contract' "
        "AND column_name = 'personal_data_policy_revision_ids'").fetchone()
    assert row is not None, "the contract-side column is missing"
    assert row[0] == "jsonb" and row[2] == "NO"
    assert "[]" in (row[1] or "")


def test_1056_pointer_version_starts_at_one(db) -> None:
    _approve(db, revision_id="pup_" + "f" * 64, content_hash="f" * 64)
    with pytest.raises(Exception):   # noqa: B017
        _point(db, revision_id="pup_" + "f" * 64, pointer_version=0)


def test_1056_has_no_confirmer_column_because_the_decision_is_single_approver(db) -> None:
    """D14, pinned as a SHAPE rather than as prose. A nullable `confirmed_by` would read as a step
    somebody forgot; its ABSENCE is what records that nobody is asked for one. If a later slice
    re-adds four-eyes it will have to change this test, which is the point — the deviation was an
    explicit user decision and must not be undone by accident.

    Asserted against the live columns, not against the file's text: the file EXPLAINS why there is
    no confirmer, so a text scan would fail on its own rationale."""
    columns = {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'pii_use_policy_revision'").fetchall()}
    assert "approved_by" in columns and "approved_at" in columns
    assert not any("confirm" in name for name in columns), columns


def test_1056_applies_with_its_real_neighbours_and_pins_none_of_the_parallel_reservations() -> None:
    names = {p.name for p in _MIGRATION_DIR.glob("*.sql")}
    present = {n.split("_", 1)[0] for n in names}
    assert {"1047", "1048", "1049", "1050", "1051", "1052", "1056"} <= present
    # 1053-1055 belong to the PARALLEL session and are deliberately not asserted either way: 1056
    # depends on nothing they would create, and pinning their absence would fail the moment that
    # session merges.


def test_1056_is_the_only_number_this_stream_allocated() -> None:
    """This slice owns 1056 and nothing else (D14/D7) — asserted as THIS stream's claim.

    Exactly the files it named, exactly once each. A SECOND `1056_*` file is the unrecorded
    allocation the full-filename ledger rule exists to catch. It deliberately says nothing about
    1053-1055, which belong to a neighbour."""
    numbered = [(p.name.split("_", 1)[0], p.name)
                for p in _MIGRATION_DIR.glob("*.sql") if p.name.split("_", 1)[0].isdigit()]
    owned = {name.split("_", 1)[0] for name in _STREAM_MIGRATIONS}
    assert sorted(name for number, name in numbered if number in owned) == sorted(
        _STREAM_MIGRATIONS)
