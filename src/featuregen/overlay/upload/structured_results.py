"""Shared content-addressed persistence for validated structured workflow results.

The immutable ``llm_call`` audit answers how a provider invocation happened.  This module answers
which validated typed result a workflow consumed.  It is intentionally generic: bridge criticism is
the first user, not the owner of a bridge-specific replay cache.

**The subject/current pointer (semantic Task 5, migration 1046).** Content-addressed immutability
answers "what did this exact input produce?" and CANNOT answer "what is current for this subject?"
without scanning ``output_json``. :func:`set_current_structured_result` adds one mutable pointer row
per ``(subject_kind, subject_ref, result_type)`` naming the current revision — the same
immutable-revision + CAS-pointer split ``governed_candidate_current`` uses (1033/1038). The
revisions stay authoritative; the pointer is derived, rebuildable, and carries no payload.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from featuregen.overlay.field_evidence import canonical_hash

STRUCTURED_RESULT_STORE_VERSION = "1.0.0"


class StructuredResultCorruption(RuntimeError):
    """A content-addressed identity already names different immutable bytes."""


class StructuredResultPointerConflict(RuntimeError):
    """A concurrent writer advanced the subject/current pointer under this caller (CAS lost).

    The loser must RE-READ and decide again — it must never overwrite, because the winner may have
    made a NEWER derivation current and a blind overwrite would silently resurrect stale semantics.
    """


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


# ── subject/current pointer (migration 1046) ────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StructuredResultPointerV1:
    """Which immutable revision is CURRENT for one subject. Carries no payload by design — the
    content lives in the referenced revision, so a pointer can never disagree with the answer."""

    subject_kind: str
    subject_ref: str
    result_type: str
    structured_result_id: str
    pointer_version: int
    lifecycle: str


def _pointer_row(row) -> StructuredResultPointerV1:
    return StructuredResultPointerV1(row[0], row[1], row[2], row[3], int(row[4]), row[5])

_POINTER_COLUMNS = ("subject_kind, subject_ref, result_type, structured_result_id, "
                    "pointer_version, lifecycle")


def load_current_structured_result(
    conn, *, subject_kind: str, subject_ref: str, result_type: str,
) -> StructuredResultPointerV1 | None:
    """The subject's pointer row, WITHDRAWN ONES INCLUDED — a caller that wants only live answers
    checks ``lifecycle``. Returning a withdrawn pointer is deliberate: supersession is information
    (a gap suggestion that was retired is not the same as one that never existed)."""
    row = conn.execute(
        f"SELECT {_POINTER_COLUMNS} FROM structured_result_current "
        "WHERE subject_kind=%s AND subject_ref=%s AND result_type=%s",
        (subject_kind, subject_ref, result_type),
    ).fetchone()
    return None if row is None else _pointer_row(row)


def list_current_structured_results(
    conn, *, subject_kind: str, result_type: str, subject_refs: Sequence[str] | None = None,
) -> tuple[StructuredResultPointerV1, ...]:
    """Every LIVE pointer of one (subject_kind, result_type), optionally narrowed to ``subject_refs``.

    The discovery query the ontology-gap review surface runs. It reads pointer rows only — never
    ``output_json`` — which is the whole reason the pointer exists."""
    sql = (f"SELECT {_POINTER_COLUMNS} FROM structured_result_current "
           "WHERE subject_kind=%s AND result_type=%s AND lifecycle='current'")
    params: list[object] = [subject_kind, result_type]
    if subject_refs is not None:
        sql += " AND subject_ref = ANY(%s)"
        params.append(list(subject_refs))
    sql += " ORDER BY subject_ref"
    return tuple(_pointer_row(row) for row in conn.execute(sql, tuple(params)).fetchall())


def set_current_structured_result(
    conn,
    *,
    subject_kind: str,
    subject_ref: str,
    result_type: str,
    structured_result_id: str,
    expected_pointer_version: int | None = None,
) -> StructuredResultPointerV1:
    """Make ``structured_result_id`` the current revision for one subject, by CAS.

    IDEMPOTENT under retry: re-pointing a LIVE pointer at the revision it already names returns it
    unchanged (no version churn, so a retried ingest does not manufacture supersession events); a
    WITHDRAWN pointer naming that same revision is revived with one version advance. Otherwise the
    pointer advances from ``expected_pointer_version`` (defaulting to the version this call just
    read) and a lost race raises :class:`StructuredResultPointerConflict` rather than overwriting —
    the 1033/1038 rule: work that started against an older snapshot may not clobber a newer answer.

    Never SELECT-then-INSERT: the create path is an ``ON CONFLICT DO NOTHING`` insert whose
    ``rowcount`` IS the CAS outcome, so two concurrent creators cannot both believe they won."""
    if not subject_kind.strip() or not subject_ref.strip() or not result_type.strip():
        raise ValueError("pointer subject kind/ref and result type must not be blank")
    current = load_current_structured_result(
        conn, subject_kind=subject_kind, subject_ref=subject_ref, result_type=result_type)
    if current is not None and current.structured_result_id == structured_result_id:
        if (expected_pointer_version is not None
                and expected_pointer_version != current.pointer_version):
            raise StructuredResultPointerConflict(
                f"pointer {subject_kind}:{subject_ref}:{result_type} advanced")
        if current.lifecycle == "current":
            return current
    expected = (
        (0 if current is None else current.pointer_version)
        if expected_pointer_version is None
        else expected_pointer_version
    )
    if expected == 0:
        changed = conn.execute(
            "INSERT INTO structured_result_current "
            "(subject_kind, subject_ref, result_type, structured_result_id, pointer_version, "
            " lifecycle) VALUES (%s,%s,%s,%s,1,'current') ON CONFLICT DO NOTHING",
            (subject_kind, subject_ref, result_type, structured_result_id),
        ).rowcount
        version = 1
    else:
        changed = conn.execute(
            "UPDATE structured_result_current "
            "SET structured_result_id=%s, pointer_version=pointer_version+1, "
            "    lifecycle='current', updated_at=now() "
            "WHERE subject_kind=%s AND subject_ref=%s AND result_type=%s AND pointer_version=%s",
            (structured_result_id, subject_kind, subject_ref, result_type, expected),
        ).rowcount
        version = expected + 1
    if changed != 1:
        raise StructuredResultPointerConflict(
            f"pointer {subject_kind}:{subject_ref}:{result_type} advanced")
    return StructuredResultPointerV1(subject_kind, subject_ref, result_type,
                                     structured_result_id, version, "current")


def withdraw_current_structured_result(
    conn, *, subject_kind: str, subject_ref: str, result_type: str,
) -> StructuredResultPointerV1 | None:
    """Retire a subject's live pointer (a re-derivation that no longer makes the claim).

    Returns the withdrawn pointer, or ``None`` when there was nothing live to withdraw. The row and
    its revision reference SURVIVE — withdrawal is a lifecycle flip, never a delete, so the audit
    can still answer what was retired. Idempotent: withdrawing an already-withdrawn pointer is a
    no-op returning ``None``."""
    current = load_current_structured_result(
        conn, subject_kind=subject_kind, subject_ref=subject_ref, result_type=result_type)
    if current is None or current.lifecycle != "current":
        return None
    changed = conn.execute(
        "UPDATE structured_result_current SET lifecycle='withdrawn', "
        "pointer_version=pointer_version+1, updated_at=now() "
        "WHERE subject_kind=%s AND subject_ref=%s AND result_type=%s AND pointer_version=%s "
        "AND lifecycle='current'",
        (subject_kind, subject_ref, result_type, current.pointer_version),
    ).rowcount
    if changed != 1:
        raise StructuredResultPointerConflict(
            f"pointer {subject_kind}:{subject_ref}:{result_type} advanced")
    return StructuredResultPointerV1(subject_kind, subject_ref, result_type,
                                     current.structured_result_id,
                                     current.pointer_version + 1, "withdrawn")
