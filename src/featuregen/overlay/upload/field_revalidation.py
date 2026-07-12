"""Human-confirmation revalidation store (spec §6.3, review must-fix #4).

A source re-upload must NEVER stale human-confirmed evidence. But when a re-upload changes a column's
MATERIAL (its definition / type), a prior human confirmation for one of that column's fields is no
longer necessarily valid — the meaning it vouched for moved. Rather than silently keep serving the
now-questionable human value (fail-open) or destroy it (losing the human's work), this store records a
PENDING revalidation flag for the ``(logical_ref, field_name)``.

:func:`active_disqualifiers_for` turns a pending flag into
``{overlay.field_authority.Disqualifier.CONFIRMATION_PENDING_REVALIDATION}`` — the ACTIVE-set
disqualifier Task 8's resolver (:func:`overlay.field_authority.resolve_field_authority`) consumes to
BLOCK the load-bearing value until a human re-confirms. The human evidence itself stays ACTIVE; only
its load-bearing effect is gated. This is the concrete backing the review asked for (a real store, not
a derived guess), keyed on the same schema-preserving ``logical_ref`` every per-field store uses.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.aggregates.ids import mint_id
from featuregen.contracts import DbConn
from featuregen.overlay.field_authority import Disqualifier


def flag_pending_revalidation(
    conn: DbConn,
    *,
    logical_ref: str,
    field_name: str,
    reason: str,
    source_snapshot_id: str,
    now: datetime | None = None,
) -> str:
    """Flag ``(logical_ref, field_name)`` PENDING human revalidation; return the minted ``frv_`` id.

    Called on a re-upload whose MATERIAL (definition / type) changed for a column that already carries
    a human-confirmed decision/evidence. The human evidence is NOT staled (a source re-upload never
    stales human evidence — that guard lives in :func:`overlay.field_evidence.stale_source_evidence`,
    which is producer-scoped); instead the load-bearing value is blocked until a human re-confirms.
    The block is enforced by :func:`active_disqualifiers_for` returning
    ``CONFIRMATION_PENDING_REVALIDATION``, which the field's policy honours.

    IDEMPOTENT per ``(logical_ref, field_name, status='pending')`` (Task-10 Minor-6): a repeated
    material-changed re-upload re-flagging the same field must not accumulate duplicate pending rows.
    An existing pending flag is returned as-is (``active_disqualifiers_for`` already blocks on it), so
    the flag stays a single row until a human clears it."""
    now = now or datetime.now(UTC)
    existing = conn.execute(
        "SELECT revalidation_id FROM field_revalidation "
        "WHERE logical_ref = %s AND field_name = %s AND status = 'pending' LIMIT 1",
        (logical_ref, field_name),
    ).fetchone()
    if existing is not None:
        return existing[0]   # already pending — idempotent, don't mint a duplicate
    revalidation_id = mint_id("frv")
    conn.execute(
        """
        INSERT INTO field_revalidation
            (revalidation_id, logical_ref, field_name, reason, source_snapshot_id, status, created_at)
        VALUES (%s, %s, %s, %s, %s, 'pending', %s)
        """,
        (revalidation_id, logical_ref, field_name, reason, source_snapshot_id, now),
    )
    return revalidation_id


def active_disqualifiers_for(
    conn: DbConn, logical_ref: str, field_name: str
) -> frozenset[Disqualifier]:
    """The disqualifier set the resolver must apply for ``(logical_ref, field_name)`` (spec §6.2/§6.3).

    Returns ``{CONFIRMATION_PENDING_REVALIDATION}`` when a PENDING revalidation row exists for the
    field (a human confirmation invalidated by a later material change), else the empty set. This is
    the seam :func:`overlay.field_authority.resolve_field_authority` consumes as its
    ``active_disqualifiers`` so a pending flag actually blocks the load-bearing value."""
    row = conn.execute(
        "SELECT 1 FROM field_revalidation "
        "WHERE logical_ref = %s AND field_name = %s AND status = 'pending' LIMIT 1",
        (logical_ref, field_name),
    ).fetchone()
    if row is None:
        return frozenset()
    return frozenset({Disqualifier.CONFIRMATION_PENDING_REVALIDATION})
