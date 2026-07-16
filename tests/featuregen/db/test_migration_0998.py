from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

# Design #3 (deferred provenance piece): per-object run provenance. 0998 creates the two
# read-model association tables — ingestion_run_object (observed/changed catalog objects) and
# ingestion_run_fact (asserted/changed overlay facts) — as CHILDREN of the 0994 ingestion_run
# manifest. PostgreSQL enforces the invariants: the closed relation vocabulary per table, the FK
# to a real run, and per-(run, ref, relation) uniqueness so a batched re-record (ON CONFLICT DO
# NOTHING at the writer) can never duplicate an association. The reserved overlay event `run_id`
# column is NOT involved anywhere here — provenance keys on a dedicated ingestion_run_id.

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _run(conn, run_id: str = "ingrun_P1") -> str:
    conn.execute(
        "INSERT INTO ingestion_run (id, origin_type, catalog_source, actor_subject, status, "
        "started_at, heartbeat_at) VALUES (%s, 'upload', 'deposits', 'user:tester', "
        "'in_progress', %s, %s)", (run_id, _NOW, _NOW))
    return run_id


def _object(conn, run_id: str, ref: str = "public.accounts.id", *,
            relation: str = "observed") -> None:
    conn.execute(
        "INSERT INTO ingestion_run_object (ingestion_run_id, catalog_source, object_ref, "
        "relation, at) VALUES (%s, 'deposits', %s, %s, %s)", (run_id, ref, relation, _NOW))


def _fact(conn, run_id: str, fact_key: str = "fk_grain_1", *,
          relation: str = "asserted") -> None:
    conn.execute(
        "INSERT INTO ingestion_run_fact (ingestion_run_id, fact_key, relation, at) "
        "VALUES (%s, %s, %s, %s)", (run_id, fact_key, relation, _NOW))


def test_object_relations_accepted(conn) -> None:
    run_id = _run(conn)
    _object(conn, run_id, "public.accounts", relation="observed")
    _object(conn, run_id, "public.accounts.id", relation="observed")
    _object(conn, run_id, "public.accounts.id", relation="changed")   # both relations, one ref


def test_fact_relations_accepted(conn) -> None:
    run_id = _run(conn)
    _fact(conn, run_id, "fk_grain_1", relation="asserted")
    _fact(conn, run_id, "fk_grain_1", relation="changed")


def test_object_relation_vocabulary_closed(conn) -> None:
    run_id = _run(conn)
    conn.execute("SELECT 1")   # open the outer tx before the savepoint (0993/0994 pattern)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _object(conn, run_id, relation="asserted")   # a FACT relation, not an object one
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _object(conn, run_id, relation="")


def test_fact_relation_vocabulary_closed(conn) -> None:
    run_id = _run(conn)
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _fact(conn, run_id, relation="observed")     # an OBJECT relation, not a fact one
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _fact(conn, run_id, relation="")


def test_associations_require_a_real_run(conn) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _object(conn, "ingrun_MISSING")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        _fact(conn, "ingrun_MISSING")


def test_object_association_unique_per_run_ref_relation(conn) -> None:
    run_id = _run(conn)
    _object(conn, run_id, "public.accounts.id", relation="observed")
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _object(conn, run_id, "public.accounts.id", relation="observed")
    # a second RUN observing the same object is a different association — fine
    other = _run(conn, "ingrun_P2")
    _object(conn, other, "public.accounts.id", relation="observed")


def test_fact_association_unique_per_run_key_relation(conn) -> None:
    run_id = _run(conn)
    _fact(conn, run_id, "fk_grain_1", relation="asserted")
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _fact(conn, run_id, "fk_grain_1", relation="asserted")
    other = _run(conn, "ingrun_P2")
    _fact(conn, other, "fk_grain_1", relation="asserted")
