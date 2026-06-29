from __future__ import annotations

import pytest

from featuregen.contracts.documents import NewDocument
from featuregen.documents.store import DagViolationError, append_document, get_document
from featuregen.ids import new_id


def _doc(provenance, *, stage="CONFIRMED_CONTRACT", derived_from=(), supersedes=()):
    return NewDocument(
        doc_id=new_id("doc"),
        stage=stage,
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:x",
        body_classification="governance-retained",
        provenance=provenance,
        body_ref="blob_1",
        derived_from=tuple(derived_from),
        supersedes=tuple(supersedes),
    )


def test_derived_from_committed_doc_is_accepted(db, actor, provenance):
    draft = append_document(
        db, _doc(provenance, stage="DRAFT_CONTRACT"), run_id="run_1", actor=actor
    )
    confirmed = append_document(
        db, _doc(provenance, derived_from=(draft,)), run_id="run_1", actor=actor
    )
    assert get_document(db, confirmed)["derived_from"] == [draft]


def test_derived_from_unknown_doc_is_rejected(db, actor, provenance):
    with pytest.raises(DagViolationError):
        append_document(
            db, _doc(provenance, derived_from=("doc_does_not_exist",)),
            run_id="run_1", actor=actor,
        )


def test_supersedes_unknown_doc_is_rejected(db, actor, provenance):
    with pytest.raises(DagViolationError):
        append_document(
            db, _doc(provenance, supersedes=("doc_ghost",)),
            run_id="run_1", actor=actor,
        )


def test_rejecting_bad_edge_inserts_nothing(db, actor, provenance):
    before = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    with pytest.raises(DagViolationError):
        append_document(
            db, _doc(provenance, derived_from=("doc_ghost",)),
            run_id="run_1", actor=actor,
        )
    after = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert after == before
