from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Optional

from psycopg.types.json import Jsonb

from sp0.contracts import DbConn, IdentityEnvelope
from sp0.contracts.documents import NewDocument

_GET_COLUMNS = (
    "doc_id", "global_seq", "request_id", "feature_id", "run_id", "stage",
    "schema_version", "branch_role", "derived_from", "supersedes", "body_ref",
    "content_hash", "body_classification", "reject_reason",
)


def compute_content_hash(body: bytes) -> str:
    """Content-address a body: 'sha256:<hex>' (§3.4)."""
    return "sha256:" + hashlib.sha256(body).hexdigest()


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
    opaque-by-reference (body_ref + content_hash); structural and DAG validation are added
    in Tasks 4-5."""
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
