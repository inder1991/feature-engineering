from __future__ import annotations

import psycopg
import pytest

from featuregen.contracts.documents import NewDocument
from featuregen.documents.store import (
    DocumentValidationError,
    append_document,
)
from featuregen.ids import new_id


def _doc(provenance, **over):
    base = dict(
        doc_id=new_id("doc"),
        stage="DRAFT_CONTRACT",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:x",
        body_classification="pii-erasable",
        provenance=provenance,
        body_ref="blob_1",
    )
    base.update(over)
    return NewDocument(**base)


def test_unknown_stage_rejected(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(db, _doc(provenance, stage="NOT_A_STAGE"), run_id="r", actor=actor)


def test_unknown_branch_role_rejected(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(db, _doc(provenance, branch_role="winner"), run_id="r", actor=actor)


def test_unknown_classification_rejected(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(db, _doc(provenance, body_classification="secret"), run_id="r", actor=actor)


def test_rejected_requires_reject_reason(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(db, _doc(provenance, branch_role="rejected"), run_id="r", actor=actor)


def test_rejected_with_reason_is_accepted(db, actor, provenance):
    doc_id = append_document(
        db,
        _doc(provenance, branch_role="rejected", reject_reason="dup of feat_9"),
        run_id="r",
        actor=actor,
    )
    assert doc_id.startswith("doc_")


def test_branch_role_is_immutable_after_commit(db, actor, provenance):
    doc_id = append_document(db, _doc(provenance), run_id="r", actor=actor)
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute("UPDATE documents SET branch_role='primary' WHERE doc_id=%s", (doc_id,))
