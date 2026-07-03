"""The SP-2 write-once blob store (fix F1, P1-b / P2-c). A minimal durable key→content store so refs
that intake mints — candidate/draft document `body_ref`s and the `raw_input_ref` — become resolvable
later. Candidate bodies are NOT event-inlined, so binding the chosen candidate + audit/replay need a
durable resolver; the raw intent is held BY REFERENCE only (§9.4 — never sent to the LLM) and this is
its audit-of-record. Writes are idempotent on identical content and physically write-once (the
`blob_no_mutation` trigger in 0511_blob_store.sql rejects any UPDATE/DELETE)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from psycopg.types.json import Jsonb

from featuregen.contracts.db import DbConn


class BlobConflictError(Exception):
    """Raised when a `blob_ref` is re-written with DIFFERENT content — the write-once contract is
    violated. An identical re-write is a benign no-op (idempotent), never this error."""


def _canonical_bytes(content: Mapping[str, Any]) -> bytes:
    """Canonical JSON encoding — stable key order, no whitespace — so the content hash is
    deterministic across writers (mirrors documents/store.py + _persist_contract_body)."""
    return json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_blob_hash(content: Mapping[str, Any]) -> str:
    """Content-address a blob payload: 'sha256:<hex>' over its canonical JSON (§3.4)."""
    return "sha256:" + hashlib.sha256(_canonical_bytes(content)).hexdigest()


def write_blob(conn: DbConn, blob_ref: str, content: Mapping[str, Any]) -> None:
    """Durably persist `content` under `blob_ref` in the write-once blob store, inside the caller's
    OPEN transaction. Idempotent: a repeat write of byte-identical content is a no-op (ON CONFLICT DO
    NOTHING — no UPDATE, so the write-once trigger never fires). A repeat write of DIFFERENT content
    raises `BlobConflictError` (the ref is already bound; write-once is not silently overwritten)."""
    content_hash = compute_blob_hash(content)
    inserted = conn.execute(
        "INSERT INTO blob (blob_ref, content, content_hash) VALUES (%s, %s, %s) "
        "ON CONFLICT (blob_ref) DO NOTHING RETURNING blob_ref",
        (blob_ref, Jsonb(dict(content)), content_hash),
    ).fetchone()
    if inserted is not None:
        return  # freshly stored
    existing = conn.execute(
        "SELECT content_hash FROM blob WHERE blob_ref = %s", (blob_ref,)
    ).fetchone()
    if existing is not None and existing[0] != content_hash:
        raise BlobConflictError(
            f"blob {blob_ref!r} already stored with different content "
            f"({existing[0]} != {content_hash}); the blob store is write-once"
        )


def read_blob(conn: DbConn, blob_ref: str) -> dict | None:
    """Resolve a `blob_ref` to its stored content (a dict), or None if unknown. The jsonb `content`
    column deserializes to a Python dict on read (psycopg default)."""
    row = conn.execute(
        "SELECT content FROM blob WHERE blob_ref = %s", (blob_ref,)
    ).fetchone()
    return row[0] if row is not None else None
