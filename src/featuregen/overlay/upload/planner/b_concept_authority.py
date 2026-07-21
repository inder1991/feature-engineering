"""Phase 3C.2b-i-B · Task 5 — the CONCEPT-AUTHORITY resolver (human-confirmed cohort).

B may only build a governed operand from an AUTHORITATIVE concept. But ``concept`` is
RECOMMENDATION-capped in the generic field-authority machine:
:func:`overlay.field_authority.resolve_field_authority` returns ``load_bearing_value=None`` for
``concept`` no matter the evidence (its ``influence_max`` sits below ``OPERATIONAL`` — locked by
``test_field_resolution``). So B resolves its OWN planner-concept authority DIRECTLY over the raw
``field_evidence`` rows, accepting ONLY ``(HUMAN, CONFIRMED)`` or ``(SOURCE, ATTESTED)`` evidence
and failing CLOSED on everything else. The demo cohort is human-confirmed concept (nothing attests
``concept`` in production today).

This is a BESPOKE resolver — it does NOT wrap/fork ``resolve_field_authority`` /
``resolve_and_project`` / ``is_feature_eligible`` (all three structurally return non-authoritative
for ``concept``). It reads the ACTIVE accepted set with the shared
:func:`overlay.field_evidence.read_active_field_evidence`; the human tier takes precedence over the
source tier (a differing source value is a lower-authority DIAGNOSTIC, not a conflict); and, only
when there is NO active accepted row, it queries the accepted HISTORY across every lifecycle to tell
``missing`` (never had it) from ``stale`` (had it, now stale/superseded). There is NO
``expected_concept`` parameter anywhere: the resolver reports what the governed evidence says — it
does not check that against a caller's claim.

Shadow-only; NO data plane. Read-only over the evidence / revalidation / graph stores.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

import psycopg

from featuregen.contracts import DbConn
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.field_authority import Disqualifier
from featuregen.overlay.field_evidence import (
    FieldEvidence,
    canonical_hash,
    read_active_field_evidence,
)
from featuregen.overlay.upload.concepts import concept as concept_record
from featuregen.overlay.upload.field_revalidation import active_disqualifiers_for
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.planner.b_dispositions import BDisposition

CONCEPT_AUTHORITY_POLICY_VERSION = "3c2bib.concept.1.0.0"

_CONCEPT_FIELD = "concept"

# The two accepted ``(producer, strength)`` pairs — the ONLY evidence this resolver treats as
# authoritative for ``concept``. An ORDERED tuple (not a set) so the history query's per-pair WHERE
# clause and its params stay index-aligned; membership tests over two elements are trivial.
_ACCEPTED_PAIRS: tuple[tuple[EvidenceProducer, AssertionStrength], ...] = (
    (EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED),
    (EvidenceProducer.SOURCE, AssertionStrength.ATTESTED),
)

# Non-blocking diagnostics attached to a returned binding (telemetry §10).
DIAGNOSTIC_DISPLAY_CONCEPT_MISMATCH = "DISPLAY_CONCEPT_MISMATCH"
DIAGNOSTIC_LOWER_AUTHORITY_DISAGREEMENT = "lower_authority_disagreement"


class ConceptAuthority(StrEnum):
    """Which accepted evidence tier produced the winning value."""

    human_confirmed = "human_confirmed"
    source_attested = "source_attested"


class ConceptAuthorityReason(StrEnum):
    """The FINE rejection reason kept on :class:`ConceptRejection` for telemetry (§10)."""

    concept_authority_missing = "concept_authority_missing"
    concept_authority_conflict = "concept_authority_conflict"
    concept_evidence_stale = "concept_evidence_stale"
    concept_revalidation_pending = "concept_revalidation_pending"
    concept_not_in_registry = "concept_not_in_registry"
    technical_failure = "technical_failure"


@dataclass(frozen=True, slots=True)
class PlannerConceptBinding:
    """A governed concept authority: the winning value, which accepted tier won, the winning rows'
    evidence ids, and the two deterministic hashes (over the sorted ids and over the value)."""

    authoritative_concept: str
    authority: ConceptAuthority
    evidence_ids: tuple[str, ...]
    evidence_set_hash: str
    value_hash: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConceptRejection:
    """A fail-closed non-binding, carrying the FINE reason for telemetry."""

    reason: ConceptAuthorityReason


# The FOLD onto the coarse ``BDisposition`` vocabulary. BDisposition has NO separate
# revalidation-pending member BY DESIGN, so BOTH ``concept_evidence_stale`` AND
# ``concept_revalidation_pending`` fold to ``concept_authority_stale`` — a documented two-into-one.
_REASON_TO_B_DISPOSITION: dict[ConceptAuthorityReason, BDisposition] = {
    ConceptAuthorityReason.concept_authority_missing: BDisposition.concept_authority_missing,
    ConceptAuthorityReason.concept_authority_conflict: BDisposition.concept_authority_conflict,
    ConceptAuthorityReason.concept_evidence_stale: BDisposition.concept_authority_stale,
    ConceptAuthorityReason.concept_revalidation_pending: BDisposition.concept_authority_stale,
    ConceptAuthorityReason.concept_not_in_registry: BDisposition.concept_not_in_registry,
    ConceptAuthorityReason.technical_failure: BDisposition.technical_failure,
}


def reason_to_b_disposition(r: ConceptAuthorityReason) -> BDisposition:
    """Fold the FINE :class:`ConceptAuthorityReason` onto the coarse :class:`BDisposition`.

    Documented two-into-one fold: BOTH ``concept_evidence_stale`` AND
    ``concept_revalidation_pending`` map to ``BDisposition.concept_authority_stale`` — BDisposition
    intentionally carries no separate revalidation-pending member. The fine reason stays on
    :class:`ConceptRejection` for telemetry, so the fold never loses information at the source."""
    return _REASON_TO_B_DISPOSITION[r]


# ── internals ─────────────────────────────────────────────────────────────────────────────────


def _pair(ev: FieldEvidence) -> tuple[EvidenceProducer, AssertionStrength]:
    """The ``(producer, strength)`` axis of a stored row, as the load-bearing Phase-0 enums."""
    return (EvidenceProducer(ev.producer), AssertionStrength(ev.strength))


def _concept_value(ev: FieldEvidence) -> str:
    """The concept name a row proposes, as a ``str``. A string ``proposed_value`` passes through; a
    structured value is rendered canonically (it will simply miss the registry) — mirroring
    :func:`overlay.field_evidence.to_view` so value comparisons stay stable and deterministic."""
    value = ev.proposed_value
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash_evidence_ids(evidence_ids: tuple[str, ...]) -> str:
    """Deterministic hash over the SORTED evidence ids (order-independent set identity)."""
    return canonical_hash(sorted(evidence_ids))


def _hash_value(value: str) -> str:
    """Deterministic hash over the winning concept value."""
    return canonical_hash(value)


def _graph_concept(conn: DbConn, logical_ref: str) -> str | None:
    """The DISPLAY concept on ``graph_node`` for this ref (diagnostic only), or ``None``.

    Mind the two key forms: ``logical_ref`` is the schema-preserving
    ``source::schema.table.column``, but ``graph_node`` is keyed on the source (``catalog_source``)
    and the FLATTENED ``public.<table>.<column>`` object_ref — the graph writer
    (``overlay.upload.graph._column_ref``) always renders ``public.<table>.<column>``, collapsing
    the schema. We rebuild that key from :func:`parse_ref` + the flatten rule. Returns ``None`` for
    a table ref (no column concept) or an absent node."""
    _source, _schema, table, column = parse_ref(logical_ref)
    if column is None:
        return None
    object_ref = f"public.{table}.{column}"
    row = conn.execute(
        "SELECT concept FROM graph_node "
        "WHERE catalog_source = %s AND object_ref = %s AND kind = 'column'",
        (_source, object_ref),
    ).fetchone()
    return row[0] if row is not None else None


def _accepted_history_lifecycles(conn: DbConn, logical_ref: str) -> set[str]:
    """The lifecycles of ALL accepted-pair ``concept`` rows for this ref, ACROSS every lifecycle.

    :func:`read_active_field_evidence` excludes non-ACTIVE rows, so this direct all-lifecycle query
    is how the resolver — when there is NO active accepted row — distinguishes 'never had accepted
    evidence' (missing) from 'had it, now stale/superseded' (stale) from 'only rejected' (missing).
    The pair clause is built from constant fragments (no user input); ``logical_ref`` and the pair
    values are bound as parameters."""
    clause = " OR ".join("(producer = %s AND strength = %s)" for _ in _ACCEPTED_PAIRS)
    params: list[str] = [logical_ref]
    for producer, strength in _ACCEPTED_PAIRS:
        params.extend([producer.value, strength.value])
    # The interpolated `clause` is built purely from constant fragments (no user input); the ref
    # and the pair values are bound as parameters, so the f-string carries no injection surface.
    rows = conn.execute(
        f"SELECT lifecycle FROM field_evidence "
        f"WHERE logical_ref = %s AND field_name = 'concept' AND ({clause})",
        params,
    ).fetchall()
    return {row[0] for row in rows}


def _resolve_active(
    conn: DbConn, logical_ref: str, accepted: list[FieldEvidence]
) -> PlannerConceptBinding | ConceptRejection:
    """Resolve when there IS active accepted evidence: precedence, conflict, pending, registry."""
    human = [ev for ev in accepted if _pair(ev) == _ACCEPTED_PAIRS[0]]
    source = [ev for ev in accepted if _pair(ev) == _ACCEPTED_PAIRS[1]]

    diagnostics: list[str] = []
    if human:
        # Human tier WINS: a differing source value is a lower-authority diagnostic, not a conflict.
        authority = ConceptAuthority.human_confirmed
        winning_rows = human
        distinct = {_concept_value(ev) for ev in human}
        if len(distinct) > 1:
            return ConceptRejection(ConceptAuthorityReason.concept_authority_conflict)
        winning_value = next(iter(distinct))
        source_values = {_concept_value(ev) for ev in source}
        if source_values and source_values != {winning_value}:
            diagnostics.append(DIAGNOSTIC_LOWER_AUTHORITY_DISAGREEMENT)
    else:
        authority = ConceptAuthority.source_attested
        winning_rows = source
        distinct = {_concept_value(ev) for ev in source}
        if len(distinct) > 1:
            return ConceptRejection(ConceptAuthorityReason.concept_authority_conflict)
        winning_value = next(iter(distinct))

    # Pending-revalidation BLOCK — used ONLY here (the resolver detects conflict itself, and must
    # NOT consult active_disqualifiers_for for conflict detection).
    if Disqualifier.CONFIRMATION_PENDING_REVALIDATION in active_disqualifiers_for(
        conn, logical_ref, _CONCEPT_FIELD
    ):
        return ConceptRejection(ConceptAuthorityReason.concept_revalidation_pending)

    # Registry check — the winning value must be a known concept.
    if concept_record(winning_value) is None:
        return ConceptRejection(ConceptAuthorityReason.concept_not_in_registry)

    # Non-blocking DISPLAY_CONCEPT_MISMATCH diagnostic (still returns the binding).
    graph_concept = _graph_concept(conn, logical_ref)
    if graph_concept is not None and graph_concept != winning_value:
        diagnostics.append(DIAGNOSTIC_DISPLAY_CONCEPT_MISMATCH)

    evidence_ids = tuple(sorted(ev.evidence_id for ev in winning_rows))
    return PlannerConceptBinding(
        authoritative_concept=winning_value,
        authority=authority,
        evidence_ids=evidence_ids,
        evidence_set_hash=_hash_evidence_ids(evidence_ids),
        value_hash=_hash_value(winning_value),
        diagnostics=tuple(diagnostics),
    )


def _resolve_history(conn: DbConn, logical_ref: str) -> ConceptRejection:
    """Resolve when there is NO active accepted evidence: query accepted history, all lifecycles."""
    lifecycles = _accepted_history_lifecycles(conn, logical_ref)
    if not lifecycles:
        return ConceptRejection(ConceptAuthorityReason.concept_authority_missing)
    if lifecycles & {EvidenceLifecycle.STALE.value, EvidenceLifecycle.SUPERSEDED.value}:
        return ConceptRejection(ConceptAuthorityReason.concept_evidence_stale)
    # Only REJECTED (or otherwise non-active) accepted rows remain — same outcome as missing.
    return ConceptRejection(ConceptAuthorityReason.concept_authority_missing)


def _resolve(conn: DbConn, logical_ref: str) -> PlannerConceptBinding | ConceptRejection:
    accepted = [
        ev
        for ev in read_active_field_evidence(conn, logical_ref, _CONCEPT_FIELD)
        if _pair(ev) in _ACCEPTED_PAIRS
    ]
    if accepted:
        return _resolve_active(conn, logical_ref, accepted)
    return _resolve_history(conn, logical_ref)


def resolve_planner_concept_binding(
    conn: DbConn, logical_ref: str
) -> PlannerConceptBinding | ConceptRejection:
    """Resolve B's OWN concept authority for ``logical_ref`` over the raw ``field_evidence`` rows.

    Accepts ONLY ``(HUMAN, CONFIRMED)`` or ``(SOURCE, ATTESTED)`` evidence; the human tier takes
    precedence over the source tier. Fails CLOSED on everything else. There is NO ``expected_concept``
    parameter — the resolver reports what the governed evidence says, it does not check it against a
    caller claim.

    Ordered, fail-closed (design §3):
      1. Active accepted set exists → conflict / pending-revalidation / not-in-registry / bind
         (attaching non-blocking ``lower_authority_disagreement`` / ``DISPLAY_CONCEPT_MISMATCH``
         diagnostics where they apply).
      2. No active accepted row → accepted history across ALL lifecycles → missing / stale /
         (rejected-only) missing.

    A DB read failure from THIS resolver's own queries → ``technical_failure``. ONLY the psycopg
    error class is caught, so a programming bug still surfaces rather than masquerading as a
    technical outcome."""
    try:
        return _resolve(conn, logical_ref)
    except psycopg.Error:
        return ConceptRejection(ConceptAuthorityReason.technical_failure)
