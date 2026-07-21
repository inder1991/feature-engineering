"""Phase 3C.2b-i-B · Task 3 — SERVER-side trust derivation for the governed cross-catalog planner.

An LLM cross-catalog proposal is untrusted. Everything downstream gates on THREE trust inputs — which
catalogs are authorized, which bare operand names map to which catalog(s), and the confirmed target
entity — and every one of them MUST be derived server-side from the authenticated roles + the durable
confirmed scope, NEVER accepted from the caller. The Task-1 spike (``b_slice_spike.derive_request_context``)
stood these up with a test-only stand-in that simply trusts caller-supplied literals; this module is the
REAL derivation and eliminates that injection surface.

The authority guarantee is STRUCTURAL, not defensive: :func:`derive_request_context` has NO
caller-catalog and NO caller-target-entity parameter. A bare operand name resolves to a catalog ONLY
through :meth:`IdentityMapV1.sources_for`, and that map is built ONLY over
``scope.authorized_catalog_sources`` (the server read-scope boundary). So a caller-claimed catalog is
un-injectable — there is nowhere to inject it.

Shadow-only; no data plane. Composes existing PRODUCTION resolvers as-is — it never forks their
authorization logic:

* :func:`resolve_catalog_scope` (``planner/scope.py``) — the read-scope/role authorization resolver
  (``allowed_sensitivities`` over ``graph_node`` + the drift watermark). THIS is the authorization
  boundary; nothing the caller says can widen it.
* :func:`scope_for_run` (``contract/scope_records.py``) — the durable, ``generation_run_id``-keyed
  rebuild of the human-confirmed use-case scope, carrying the confirmed ``target_entity``.
* :func:`_candidate_columns` (``feature_assist.py``) — the read-scope-gated ``graph_node`` scan reused
  to build the identity map, so the sensitivity gate stays single-sourced (never re-implemented here).
* :func:`known_entities` (``taxonomy/dimensions.py``) — the closed governed target-entity vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from featuregen.overlay.upload.contract.scope_records import scope_for_run
from featuregen.overlay.upload.feature_assist import _candidate_columns
from featuregen.overlay.upload.planner.contracts import CatalogScopeV1
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.taxonomy.dimensions import known_entities


class TrustDerivationError(Exception):
    """A fail-closed reject in the server-side trust derivation. ``reason`` is a SHORT stable slug
    (``confirmed_scope_missing`` / ``target_entity_unconfirmed``) — a machine token for the caller,
    never a leaky human sentence."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IdentityEntryV1:
    """One server-derived operand identity: a bare ``object_ref`` and the AUTHORIZED catalog
    source(s) that carry it (sorted, deduplicated)."""

    object_ref: str
    catalog_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IdentityMapV1:
    """The server-derived ``object_ref -> authorized catalog source(s)`` map. Every entry is provably
    reachable ONLY through a catalog in the server-authorized scope, so a caller-claimed catalog is
    un-injectable. ``entries`` is sorted by ``object_ref`` (each entry's ``catalog_sources`` sorted),
    so the map is reproducible byte-for-byte from the same run/roles/scope."""

    entries: tuple[IdentityEntryV1, ...]

    @property
    def known(self) -> frozenset[str]:
        """Every ``object_ref`` the server authorized — the closed set a bare operand must land in."""
        return frozenset(e.object_ref for e in self.entries)

    def sources_for(self, object_ref: str) -> tuple[str, ...]:
        """The authorized catalog source(s) that carry ``object_ref`` — ``()`` if unknown (a
        caller-claimed catalog can never be conjured for an operand the server never authorized)."""
        for entry in self.entries:
            if entry.object_ref == object_ref:
                return entry.catalog_sources
        return ()


@dataclass(frozen=True, slots=True)
class RequestContextV1:
    """The SERVER-derived trust inputs, bundled (mirrors the Task-1 spike's carrier): the operand
    identity map, the authorized ``CatalogScopeV1``, and the confirmed non-null ``target_entity``.
    The caller supplies NONE of these — they are the trust boundary."""

    identity_map: IdentityMapV1
    scope: CatalogScopeV1
    target_entity: str


def _build_identity_map(conn, *, scope: CatalogScopeV1, roles: tuple[str, ...]) -> IdentityMapV1:
    """Build ``object_ref -> authorized catalog source(s)`` ONLY over ``scope.authorized_catalog_sources``.

    For each authorized source, run the read-scope-gated :func:`_candidate_columns` scan (the SAME
    ``allowed_sensitivities`` gate the scope resolved under) and reduce its rows into
    ``object_ref -> {catalog_source}``. Called per authorized source with ``entity=None`` (the
    catalog-scoped branch), so every returned row's ``catalog_source`` equals the scanned source and
    an unauthorized catalog can never contribute a row. Materialized deterministically (entries sorted
    by ``object_ref``, sources sorted) so the map is reproducible."""
    by_ref: dict[str, set[str]] = {}
    for src in scope.authorized_catalog_sources:
        for row in _candidate_columns(conn, src, roles):
            by_ref.setdefault(row["object_ref"], set()).add(row["catalog_source"])
    entries = tuple(
        IdentityEntryV1(object_ref=ref, catalog_sources=tuple(sorted(sources)))
        for ref, sources in sorted(by_ref.items()))
    return IdentityMapV1(entries=entries)


def derive_request_context(conn, *, roles: tuple[str, ...], generation_run_id: str,
                           now: datetime) -> RequestContextV1:
    """Derive the governed cross-catalog trust inputs SERVER-SIDE, fail-closed.

    Steps, in order:

    1. Load the durable confirmed scope for ``generation_run_id``. Absent ->
       ``TrustDerivationError("confirmed_scope_missing")``.
    2. Require a confirmed, IN-VOCABULARY ``target_entity`` (:func:`known_entities`). ``None`` or an
       out-of-vocabulary value -> ``TrustDerivationError("target_entity_unconfirmed")``.
    3. Resolve the authorized :class:`CatalogScopeV1` from the authenticated ``roles`` — the
       authorization boundary; nothing the caller says can widen it.
    4. Build the identity map ONLY over ``scope.authorized_catalog_sources``.

    ``now`` is required because :func:`resolve_catalog_scope` joins the drift watermark to stamp the
    scope. No parameter carries a caller catalog or a caller target entity, so both are un-injectable.
    """
    confirmed = scope_for_run(conn, generation_run_id)
    if confirmed is None:
        raise TrustDerivationError("confirmed_scope_missing")
    target = confirmed.target_entity
    if target is None or target not in known_entities():
        raise TrustDerivationError("target_entity_unconfirmed")
    scope = resolve_catalog_scope(conn, roles=roles, target_entity=target, now=now)
    identity_map = _build_identity_map(conn, scope=scope, roles=roles)
    return RequestContextV1(identity_map=identity_map, scope=scope, target_entity=target)
