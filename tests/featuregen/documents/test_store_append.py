from __future__ import annotations

from featuregen.contracts.documents import NewDocument
from featuregen.documents.store import append_document, compute_content_hash, get_document
from featuregen.ids import new_id


def _candidate(provenance, content_hash="sha256:x", body_ref="blob_1", doc_id=None):
    return NewDocument(
        doc_id=doc_id or new_id("doc"),
        stage="DRAFT_CONTRACT",
        schema_version=1,
        branch_role="candidate",
        content_hash=content_hash,
        body_classification="pii-erasable",
        provenance=provenance,
        body_ref=body_ref,
    )


def test_compute_content_hash_is_deterministic_and_prefixed():
    h = compute_content_hash(b"hello")
    assert h.startswith("sha256:")
    assert h == compute_content_hash(b"hello")
    assert h != compute_content_hash(b"world")


def test_append_document_returns_doc_id_and_stores_fields(db, actor, provenance):
    doc_id = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    assert doc_id.startswith("doc_")
    row = get_document(db, doc_id)
    assert row["stage"] == "DRAFT_CONTRACT"
    assert row["branch_role"] == "candidate"
    assert row["run_id"] == "run_1"
    assert row["body_ref"] == "blob_1"
    assert row["content_hash"] == "sha256:x"
    assert row["body_classification"] == "pii-erasable"


def test_global_seq_is_monotonic_across_appends(db, actor, provenance):
    a = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    assert get_document(db, b)["global_seq"] > get_document(db, a)["global_seq"]


def test_get_document_returns_none_for_unknown(db):
    assert get_document(db, "doc_missing") is None
