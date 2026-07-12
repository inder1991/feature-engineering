"""The per-field proposal store (spec §5.1): every producer's candidate value for one object-field.

This is the write side of the authority kernel's evidence axis. Where ``overlay_evidence`` holds
metrics-oriented profiling records, ``field_evidence`` holds one immutable row per PROPOSAL a
producer makes for a single ``(logical_ref, field_name)`` — e.g. the glossary reader proposing a
``definition``, or Pass A proposing a ``concept``. Every Phase-1 producer (glossary reader, sample
parser, Pass A LLM, taxonomy) writes here; the resolver reads the ACTIVE set and picks a value with
:func:`overlay.field_authority.resolve_field_authority`.

Append-only + lifecycle-gated (never mutated in place except the lifecycle transition):
:func:`record_field_evidence` mints a fresh ``fev_`` id per proposal; a source re-upload calls
:func:`stale_source_evidence` to flip its OWN superseded rows to ``stale``. That staling is
PRODUCER-SCOPED (a review must-fix): a re-upload may only stale the producer's own drifted rows and
must NEVER stale human- or taxonomy-produced evidence. ``input_hash`` (the per-FIELD input hash from
:func:`field_input_hash`) keys reuse across snapshots — an unchanged input is neither re-written nor
re-staled.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.field_authority import FieldEvidenceView


def canonical_hash(value: object) -> str:
    """Order-independent SHA-256 of a JSON-serializable value (spec §5.1).

    ``json.dumps(value, sort_keys=True, separators=(",", ":"))`` then SHA-256 — the same convention
    as ``overlay.identity._digest``. ``sort_keys`` makes it ORDER-INDEPENDENT for mappings, so
    ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` hash identically; this stability is what lets
    staleness and decisions key on a value hash. Used for ``proposed_value_hash``."""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def field_input_hash(*, logical_ref: str, field_name: str, material: object) -> str:
    """The per-FIELD input hash that keys staleness across snapshots (spec §5.1).

    Hashes the ONE field's input ``material`` (e.g. the definition text for the ``definition`` field
    — NOT the whole source row), namespaced by ``(logical_ref, field_name)`` so two fields of the
    same row hash differently even if their material coincides. Stable for an unchanged input, so a
    re-upload whose field material did not change produces the same hash (and is not re-staled)."""
    return canonical_hash(
        {"logical_ref": logical_ref, "field_name": field_name, "material": material}
    )


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    """One immutable per-field proposal row (spec §5.1), mirroring the ``field_evidence`` columns.

    ``proposed_value`` round-trips from jsonb as the producer wrote it; ``evidence_spans`` is a
    ``tuple[str, ...]`` (immutable record contract) though it is stored as a jsonb list."""

    evidence_id: str
    logical_ref: str
    field_name: str
    proposed_value: object
    proposed_value_hash: str
    producer: str
    strength: str
    lifecycle: str
    producer_ref: str
    producer_item_ref: str | None
    producer_configuration_hash: str | None
    evidence_spans: tuple[str, ...]
    confidence_band: str | None
    source_snapshot_id: str
    input_hash: str
    created_at: object


def record_field_evidence(
    conn: DbConn,
    *,
    logical_ref: str,
    field_name: str,
    proposed_value: object,
    producer: EvidenceProducer | str,
    strength: AssertionStrength | str,
    producer_ref: str,
    source_snapshot_id: str,
    input_hash: str,
    producer_item_ref: str | None = None,
    producer_configuration_hash: str | None = None,
    evidence_spans: Sequence[str] = (),
    confidence_band: str | None = None,
    lifecycle: EvidenceLifecycle | str = EvidenceLifecycle.ACTIVE,
) -> str:
    """Record one immutable per-field proposal (spec §5.1) and return its ``fev_`` evidence_id.

    Append-only: every call mints a fresh id and INSERTs a new row — there is no update path (a
    supersession is a NEW row, plus a lifecycle flip via :func:`stale_source_evidence`).
    ``proposed_value`` is stored verbatim as jsonb and ``proposed_value_hash`` is its order-
    independent :func:`canonical_hash`. ``producer`` / ``strength`` / ``lifecycle`` accept either the
    Phase-0 enum or its string value — each is validated and normalized to its ``.value`` on the way
    in. ``evidence_spans`` persists as a jsonb list and round-trips as a tuple on read."""
    evidence_id = mint_id("fev")
    conn.execute(
        """
        INSERT INTO field_evidence
            (evidence_id, logical_ref, field_name, proposed_value, proposed_value_hash,
             producer, strength, lifecycle, producer_ref, producer_item_ref,
             producer_configuration_hash, evidence_spans, confidence_band,
             source_snapshot_id, input_hash)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            evidence_id, logical_ref, field_name, Jsonb(proposed_value),
            canonical_hash(proposed_value),
            EvidenceProducer(producer).value, AssertionStrength(strength).value,
            EvidenceLifecycle(lifecycle).value, producer_ref, producer_item_ref,
            producer_configuration_hash, Jsonb(list(evidence_spans)), confidence_band,
            source_snapshot_id, input_hash,
        ),
    )
    return evidence_id


def read_active_field_evidence(
    conn: DbConn, logical_ref: str, field_name: str
) -> list[FieldEvidence]:
    """Read the ACTIVE proposals for one ``(logical_ref, field_name)`` (spec §5.1).

    Returns only ``lifecycle == 'active'`` rows (stale / rejected / superseded are excluded) — the
    exact input the resolver reasons over. Ordered by ``created_at`` then ``evidence_id`` for a
    deterministic sequence (the resolver treats the result as a set, so order is not load-bearing)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT * FROM field_evidence
            WHERE logical_ref = %s AND field_name = %s AND lifecycle = 'active'
            ORDER BY created_at, evidence_id
            """,
            (logical_ref, field_name),
        )
        rows = cur.fetchall()
    return [
        FieldEvidence(
            evidence_id=row["evidence_id"],
            logical_ref=row["logical_ref"],
            field_name=row["field_name"],
            proposed_value=row["proposed_value"],
            proposed_value_hash=row["proposed_value_hash"],
            producer=row["producer"],
            strength=row["strength"],
            lifecycle=row["lifecycle"],
            producer_ref=row["producer_ref"],
            producer_item_ref=row["producer_item_ref"],
            producer_configuration_hash=row["producer_configuration_hash"],
            # jsonb round-trips as a list; the frozen record's contract is an immutable tuple.
            evidence_spans=tuple(row["evidence_spans"]),
            confidence_band=row["confidence_band"],
            source_snapshot_id=row["source_snapshot_id"],
            input_hash=row["input_hash"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def stale_source_evidence(
    conn: DbConn,
    *,
    logical_ref: str,
    field_name: str,
    producer: EvidenceProducer | str,
    keep_input_hash: str,
) -> int:
    """Stale a producer's drifted ACTIVE rows for a field on re-ingest; return the rows staled.

    PRODUCER-SCOPED (a review must-fix): flips to ``stale`` only the given ``producer``'s active
    rows for ``(logical_ref, field_name)`` whose ``input_hash`` differs from ``keep_input_hash``
    (the current upload's input). A source re-upload therefore stales ONLY its own superseded
    proposals and NEVER human- or taxonomy-produced evidence. An unchanged input
    (``input_hash == keep_input_hash``) is left ACTIVE — snapshot reuse keys on the hash."""
    cur = conn.execute(
        """
        UPDATE field_evidence SET lifecycle = 'stale'
        WHERE logical_ref = %s AND field_name = %s AND producer = %s
          AND lifecycle = 'active' AND input_hash <> %s
        """,
        (logical_ref, field_name, EvidenceProducer(producer).value, keep_input_hash),
    )
    return cur.rowcount


def to_view(ev: FieldEvidence) -> FieldEvidenceView:
    """Project a stored :class:`FieldEvidence` into the resolver's :class:`FieldEvidenceView`.

    Carries the producer / strength axis as the Phase-0 enums (load-bearing for selection, not raw
    strings), the proposed value, and the ``evidence_id`` for audit. A string ``proposed_value``
    passes through unchanged; a structured value is projected to its canonical JSON string so the
    resolver's value comparisons stay stable (the resolver's fields are string-valued)."""
    value = (
        ev.proposed_value
        if isinstance(ev.proposed_value, str)
        else json.dumps(ev.proposed_value, sort_keys=True, separators=(",", ":"))
    )
    return FieldEvidenceView(
        producer=EvidenceProducer(ev.producer),
        strength=AssertionStrength(ev.strength),
        value=value,
        evidence_id=ev.evidence_id,
    )
