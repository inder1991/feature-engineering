from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Optional

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn, IdentityEnvelope
from featuregen.contracts.documents import (
    BODY_CLASSIFICATIONS,
    BRANCH_ROLES,
    STAGES,
    NewDocument,
)

_GET_COLUMNS = (
    "doc_id", "global_seq", "request_id", "feature_id", "run_id", "stage",
    "schema_version", "branch_role", "derived_from", "supersedes", "body_ref",
    "content_hash", "body_classification", "reject_reason",
)


class DagViolationError(Exception):
    """Raised when derived_from/supersedes references a doc that is not already committed."""


class DocumentValidationError(Exception):
    """Raised when a NewDocument violates a structural invariant before insert."""


def _validate_structure(new_document: NewDocument) -> None:
    if new_document.stage not in STAGES:
        raise DocumentValidationError(f"unknown stage: {new_document.stage!r}")
    if new_document.branch_role not in BRANCH_ROLES:
        raise DocumentValidationError(
            f"unknown branch_role: {new_document.branch_role!r}"
        )
    if new_document.body_classification not in BODY_CLASSIFICATIONS:
        raise DocumentValidationError(
            f"unknown body_classification: {new_document.body_classification!r}"
        )
    if new_document.branch_role == "rejected" and not new_document.reject_reason:
        raise DocumentValidationError("branch_role='rejected' requires reject_reason")


def compute_content_hash(body: bytes) -> str:
    """Content-address a body: 'sha256:<hex>' (§3.4)."""
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _validate_dag(conn: DbConn, new_document: NewDocument) -> None:
    """Lineage edges may only point at already-committed docs (§3.4). Existence of a
    reference ⇒ it has a lower global_seq ⇒ the lineage DAG is acyclic by construction."""
    refs = tuple(new_document.derived_from) + tuple(new_document.supersedes)
    if not refs:
        return
    found = {
        r[0]
        for r in conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id = ANY(%s)", (list(refs),)
        ).fetchall()
    }
    missing = [r for r in refs if r not in found]
    if missing:
        raise DagViolationError(
            f"derived_from/supersedes reference uncommitted docs: {missing}"
        )


def append_document(
    conn: DbConn,
    new_document: NewDocument,
    *,
    run_id: Optional[str] = None,
    feature_id: Optional[str] = None,
    request_id: Optional[str] = None,
    actor: IdentityEnvelope,
) -> str:
    """Insert one frozen document inside the caller's OPEN transaction (§5.1).
    Uses the CALLER-SUPPLIED new_document.doc_id (minted via HandlerContext.new_doc_id())
    so events emitted in the same step can reference it; this is the single validated write
    path — runtime handlers MUST go through here, never a raw INSERT. The body is
    opaque-by-reference (body_ref + content_hash); structural invariants (stage/branch_role/
    body_classification vocab + reject_reason) are validated up front, with the DB CHECKs as
    the backstop."""
    _validate_structure(new_document)
    _validate_dag(conn, new_document)
    doc_id = new_document.doc_id
    conn.execute(
        """
        INSERT INTO documents (
            doc_id, request_id, feature_id, run_id, stage, schema_version,
            branch_role, derived_from, supersedes, body_ref, content_hash,
            body_classification, actor, provenance, reject_reason
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        """,
        (
            doc_id, request_id, feature_id, run_id, new_document.stage,
            new_document.schema_version, new_document.branch_role,
            list(new_document.derived_from), list(new_document.supersedes),
            new_document.body_ref, new_document.content_hash,
            new_document.body_classification,
            Jsonb(asdict(actor)), Jsonb(asdict(new_document.provenance)),
            new_document.reject_reason,
        ),
    )
    return doc_id


def get_document(conn: DbConn, doc_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        f"SELECT {', '.join(_GET_COLUMNS)} FROM documents WHERE doc_id = %s",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_GET_COLUMNS, row))
