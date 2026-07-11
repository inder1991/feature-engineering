"""Object identity: Layer-0 logical/provider wrappers + resolution status + no-attach guard.

COMPATIBILITY LAYER (honest framing). This module introduces the spec's Layer-0 identity seam
(``LogicalObjectRef`` / ``ProviderObjectRef`` / ``ObjectBinding`` + a resolution *status*) as a thin
wrapper OVER the existing ``CatalogObjectRef`` / ``CatalogAdapter`` machinery in
``overlay.identity`` / ``overlay.catalog``. It does NOT replace that machinery: ``fact_key`` and the
``CatalogObjectRef`` model remain the load-bearing identity for facts. This exists so later phases
have a stable identity seam to hang cross-provider / glossary resolution on.

What is built here:
  * ``classify_identity`` — a PURE candidate-count -> status classifier.
  * ``resolve_object_identity`` — derives candidates from a REAL ``CatalogAdapter`` and classifies.
  * ``may_attach`` — the write-side guard (attach only when identity is trusted).

Documented FOLLOW-UP (NOT built here):
  * ``ALIASED`` (a ref that resolves through a *confirmed rename-mapping* to a native object) is a
    valid terminal status but is never PRODUCED here — no rename-mapping store exists yet.
    ``classify_identity`` therefore only ever returns EXACT / UNRESOLVED / AMBIGUOUS; ``may_attach``
    already accepts ALIASED so the guard needs no change when that resolution lands.
  * ``AMBIGUOUS`` (>1 native candidate for one logical ref) CANNOT arise from today's single
    Postgres ``CatalogAdapter`` — one adapter resolves a ref to 0 or 1 native object. It is a FUTURE
    glossary / cross-provider case; the pure classifier carries the branch so it is testable now.
  * ``ProviderObjectRef`` (provider-snapshot-scoped native reference) is the wrapper the future
    cross-provider resolver will emit; it is defined here but not yet produced by resolution.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref


class ObjectIdentityStatus(StrEnum):
    """Outcome of resolving a logical object ref to native provider identity.

    ``EXACT`` — resolved to exactly one native object. ``ALIASED`` — resolved via a confirmed
    rename-mapping (FOLLOW-UP; not produced yet). ``AMBIGUOUS`` — more than one native candidate
    (FUTURE cross-provider case; unreachable from a single Postgres adapter). ``UNRESOLVED`` — no
    native candidate.
    """

    EXACT = "exact"
    ALIASED = "aliased"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class LogicalObjectRef:
    """A catalog-scoped LOGICAL object identity (what a human/consumer names), independent of the
    provider's native ids. ``logical_catalog_id`` is the logical namespace the ref lives in — mapped
    from the adapter's ``catalog_source`` by ``resolve_object_identity``."""

    logical_catalog_id: str
    schema: str
    table: str
    column: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderObjectRef:
    """A PROVIDER-scoped native object reference, pinned to one provider snapshot. Emitted by the
    future cross-provider resolver; defined here as the Layer-0 seam (not yet produced)."""

    provider_id: str
    provider_snapshot_id: str
    native_ref: str


@dataclass(frozen=True, slots=True)
class ObjectBinding:
    """The result of resolving a ref: the logical identity to attach to (``None`` unless trusted),
    the resolution ``status``, and the raw native ``candidates`` that produced it."""

    logical_ref: LogicalObjectRef | None
    status: ObjectIdentityStatus
    candidates: tuple[str, ...]


def classify_identity(candidates: tuple[str, ...]) -> ObjectIdentityStatus:
    """PURE classifier: map a native-candidate count to a resolution status.

    ``1 -> EXACT``, ``0 -> UNRESOLVED``, ``>1 -> AMBIGUOUS``. Never returns ``ALIASED`` (that
    requires a confirmed rename-mapping — a FOLLOW-UP). The ``AMBIGUOUS`` branch is exercised HERE,
    not via the real adapter, because a single Postgres catalog yields 0 or 1 candidate."""
    if len(candidates) == 1:
        return ObjectIdentityStatus.EXACT
    if len(candidates) == 0:
        return ObjectIdentityStatus.UNRESOLVED
    return ObjectIdentityStatus.AMBIGUOUS


def resolve_object_identity(adapter: CatalogAdapter, ref: CatalogObjectRef) -> ObjectBinding:
    """Resolve ``ref`` to native identity via the REAL ``CatalogAdapter``, then classify.

    Candidates are derived from ``adapter.fingerprint()`` — the ``display_object_ref(ref)`` lookup
    yields at most one ``CatalogObject``; its ``native_oid`` (when present) is the single candidate,
    else there are none. A single Postgres catalog therefore produces EXACT or UNRESOLVED only.

    The ``LogicalObjectRef`` is built from the ref's ``schema``/``table``/``column`` with the
    adapter's ``catalog_source`` as ``logical_catalog_id`` (the logical namespace) — and is attached
    only on ``EXACT`` (the sole trusted status resolution produces today; ``None`` otherwise so
    callers cannot attach to an unresolved identity)."""
    obj = adapter.fingerprint().get(display_object_ref(ref))
    candidates: tuple[str, ...] = (
        (obj.native_oid,) if obj is not None and obj.native_oid is not None else ()
    )
    status = classify_identity(candidates)
    logical_ref = (
        LogicalObjectRef(
            logical_catalog_id=adapter.catalog_source,
            schema=ref.schema,
            table=ref.table,
            column=ref.column,
        )
        if status is ObjectIdentityStatus.EXACT
        else None
    )
    return ObjectBinding(logical_ref=logical_ref, status=status, candidates=candidates)


def may_attach(binding: ObjectBinding) -> bool:
    """Write-side guard: attach an overlay fact to this object ONLY when its identity is trusted —
    ``EXACT`` or ``ALIASED``. ``AMBIGUOUS`` and ``UNRESOLVED`` bindings are never attachable."""
    return binding.status in (ObjectIdentityStatus.EXACT, ObjectIdentityStatus.ALIASED)
