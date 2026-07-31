"""Shared content-addressed persistence for validated structured workflow results.

The immutable ``llm_call`` audit answers how a provider invocation happened.  This module answers
which validated typed result a workflow consumed.  It is intentionally generic: bridge criticism is
the first user, not the owner of a bridge-specific replay cache.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from featuregen.overlay.field_evidence import canonical_hash

STRUCTURED_RESULT_STORE_VERSION = "1.0.0"


class StructuredResultCorruption(RuntimeError):
    """A content-addressed identity already names different immutable bytes."""


@dataclass(frozen=True, slots=True)
class StructuredResultV1:
    structured_result_id: str
    result_type: str
    result_version: int
    input_content_hash: str
    output_content_hash: str
    output: Mapping[str, Any]


def _record_provenance(
    conn,
    *,
    result_id: str,
    producer_kind: str,
    producer_ref: str,
    authority: Mapping[str, Any] | None,
) -> None:
    conn.execute(
        "INSERT INTO structured_result_provenance ("
        " structured_result_id,producer_kind,producer_ref,authority_json"
        ") VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (
            result_id,
            producer_kind,
            producer_ref,
            Jsonb(dict(authority or {})),
        ),
    )


def _identity_payload(
    *,
    result_type: str,
    result_version: int,
    input_content_hash: str,
    output_content_hash: str,
) -> dict[str, object]:
    return {
        "store_version": STRUCTURED_RESULT_STORE_VERSION,
        "result_type": result_type,
        "result_version": result_version,
        "input_content_hash": input_content_hash,
        "output_content_hash": output_content_hash,
    }


def record_structured_result(
    conn,
    *,
    result_type: str,
    result_version: int,
    input_content_hash: str,
    output: Mapping[str, Any],
    producer_kind: str,
    producer_ref: str,
    authority: Mapping[str, Any] | None = None,
) -> StructuredResultV1:
    """Record an immutable validated result and attach independently auditable provenance."""
    if not result_type.strip() or not input_content_hash.strip():
        raise ValueError("structured result type and input hash must not be blank")
    if result_version < 1:
        raise ValueError("structured result version must be positive")
    if not producer_kind.strip() or not producer_ref.strip():
        raise ValueError("structured result producer kind/ref must not be blank")
    material = dict(output)
    output_hash = canonical_hash(material)
    existing = find_structured_result(
        conn,
        result_type=result_type,
        result_version=result_version,
        input_content_hash=input_content_hash,
    )
    if existing is not None:
        if existing.output_content_hash != output_hash:
            raise StructuredResultCorruption(
                "one versioned structured-result input produced two validated outputs: "
                f"{result_type}@{result_version}:{input_content_hash}")
        _record_provenance(
            conn,
            result_id=existing.structured_result_id,
            producer_kind=producer_kind,
            producer_ref=producer_ref,
            authority=authority,
        )
        return existing
    result_id = "sres_" + canonical_hash(_identity_payload(
        result_type=result_type,
        result_version=result_version,
        input_content_hash=input_content_hash,
        output_content_hash=output_hash,
    ))
    conn.execute(
        "INSERT INTO structured_result ("
        " structured_result_id,result_type,result_version,input_content_hash,"
        " output_content_hash,output_json"
        ") VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (result_type,result_version,input_content_hash) DO NOTHING",
        (
            result_id,
            result_type,
            result_version,
            input_content_hash,
            output_hash,
            Jsonb(material),
        ),
    )
    stored = conn.execute(
        "SELECT structured_result_id,result_type,result_version,input_content_hash,"
        "output_content_hash,output_json FROM structured_result "
        "WHERE result_type=%s AND result_version=%s AND input_content_hash=%s",
        (result_type, result_version, input_content_hash),
    ).fetchone()
    expected = (
        result_id,
        result_type,
        result_version,
        input_content_hash,
        output_hash,
        material,
    )
    if stored is None or tuple(stored) != expected:
        raise StructuredResultCorruption(
            "concurrent structured result produced different validated output for "
            f"{result_type}@{result_version}:{input_content_hash}")
    _record_provenance(
        conn,
        result_id=result_id,
        producer_kind=producer_kind,
        producer_ref=producer_ref,
        authority=authority,
    )
    return StructuredResultV1(
        result_id,
        result_type,
        result_version,
        input_content_hash,
        output_hash,
        material,
    )


def load_structured_result(conn, structured_result_id: str) -> StructuredResultV1 | None:
    row = conn.execute(
        "SELECT result_type,result_version,input_content_hash,output_content_hash,output_json "
        "FROM structured_result WHERE structured_result_id=%s",
        (structured_result_id,),
    ).fetchone()
    if row is None:
        return None
    result_type, version, input_hash, output_hash, output = row
    if canonical_hash(output) != output_hash:
        raise StructuredResultCorruption(
            f"structured result content hash mismatch for {structured_result_id}")
    expected_id = "sres_" + canonical_hash(_identity_payload(
        result_type=result_type,
        result_version=int(version),
        input_content_hash=input_hash,
        output_content_hash=output_hash,
    ))
    if expected_id != structured_result_id:
        raise StructuredResultCorruption(
            f"structured result identity mismatch for {structured_result_id}")
    return StructuredResultV1(
        structured_result_id,
        result_type,
        int(version),
        input_hash,
        output_hash,
        output,
    )


def find_structured_result(
    conn,
    *,
    result_type: str,
    result_version: int,
    input_content_hash: str,
) -> StructuredResultV1 | None:
    """Replay the one canonical validated result for an exact versioned input."""
    row = conn.execute(
        "SELECT structured_result_id FROM structured_result "
        "WHERE result_type=%s AND result_version=%s AND input_content_hash=%s",
        (result_type, result_version, input_content_hash),
    ).fetchone()
    return None if row is None else load_structured_result(conn, row[0])
