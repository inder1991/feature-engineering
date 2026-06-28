from __future__ import annotations

import psycopg
import pytest


def _table_exists(db, name: str) -> bool:
    row = db.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",)
    ).fetchone()
    return bool(row[0])


def test_phase02_tables_exist(db):
    for name in ("documents", "stage_primary", "blob_index", "document_type_registry"):
        assert _table_exists(db, name), name


def _insert_doc(db, doc_id: str) -> None:
    db.execute(
        """
        INSERT INTO documents
            (doc_id, stage, schema_version, branch_role, content_hash,
             body_classification, actor, provenance)
        VALUES (%s, 'DRAFT_CONTRACT', 1, 'candidate', 'sha256:x',
                'pii-erasable', '{}'::jsonb, '{}'::jsonb)
        """,
        (doc_id,),
    )


def test_documents_are_write_once_no_update(db):
    _insert_doc(db, "doc_wo_update")
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute(
                "UPDATE documents SET branch_role='primary' WHERE doc_id='doc_wo_update'"
            )


def test_documents_are_write_once_no_delete(db):
    _insert_doc(db, "doc_wo_delete")
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute("DELETE FROM documents WHERE doc_id='doc_wo_delete'")


def test_one_live_primary_per_run_stage_is_unique(db):
    _insert_doc(db, "doc_primary_a")
    db.execute(
        "INSERT INTO stage_primary (run_id, stage, doc_id, selected_seq) "
        "VALUES ('run_1', 'DRAFT_CONTRACT', 'doc_primary_a', 1)"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO stage_primary (run_id, stage, doc_id, selected_seq) "
                "VALUES ('run_1', 'DRAFT_CONTRACT', 'doc_primary_a', 2)"
            )
