"""Governed endpoint revalidation with complete key and authority provenance.

A path endpoint is governed only when the merged resolver serves an authoritative catalog or
VERIFIED overlay ``grain`` value. Advisory ``graph_node.is_grain`` never grants keyness.

``governed_endpoint`` resolves the table's grain fact via the merged-view read API
(:func:`resolve_fact`), retains explicit uniqueness, classifies authority provenance, pins the exact
fact/content revision, and validates every declared key member against ``graph_node``. The stable
``grain_fact_key`` and revision-specific dependency identity are both carried. Missing, DRAFT,
stale, unreadable or malformed evidence returns ``None``.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from featuregen.overlay import facts
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedEndpointV1,
    GrainAuthorityProvenance,
)

_SCHEMA_SEP = "."


def _table_object_ref(catalog: str, table_ref: str) -> CatalogObjectRef:
    """Rebuild the table ``CatalogObjectRef`` from ``catalog`` + the dotted ``schema.table`` object
    ref. Built identically to ``upload_catalog.table_ref`` so ``resolve_fact``'s ``fact_key`` matches
    the one the governance write path keyed the grain fact on (``fact_key`` is deterministic over the
    normalized identity tuple)."""
    schema, _, table = table_ref.rpartition(_SCHEMA_SEP)
    return CatalogObjectRef(catalog_source=catalog, object_kind="table",
                            schema=schema, table=table, column=None)


def _authority_and_revision(
    conn,
    *,
    resolved,
    grain_fact_key: str,
) -> tuple[GrainAuthorityProvenance, str] | None:
    """Read authority from its source instead of inferring it from VERIFIED alone."""
    if resolved.source == "catalog":
        revision = "catalog:" + canonical_hash({
            "fact_key": grain_fact_key,
            "value": resolved.value,
        })
        return GrainAuthorityProvenance.catalog_authoritative, revision
    if resolved.source != "overlay":
        return None
    event_id = (resolved.provenance or {}).get("confirmed_event_id")
    if not isinstance(event_id, str) or not event_id:
        return None
    try:
        stream = tuple(load_fact(conn, grain_fact_key))
    except Exception:  # noqa: BLE001 - unreadable provenance makes the endpoint ungoverned
        return None
    confirmed = next((event for event in stream if event.event_id == event_id), None)
    if confirmed is None or confirmed.type != facts.OVERLAY_FACT_CONFIRMED:
        return None
    if confirmed.payload.get("authority_basis") == facts.AUTHORITY_SOURCE_DECLARED:
        authority = GrainAuthorityProvenance.source_declared
    elif confirmed.actor.actor_kind == "human" and confirmed.payload.get("confirmers"):
        authority = GrainAuthorityProvenance.human_confirmed
    else:
        authority = GrainAuthorityProvenance.legacy_unspecified
    return authority, event_id


def governed_endpoint(conn, adapter: CatalogAdapter, *, catalog: str, table_ref: str,
                      now: datetime) -> GovernedEndpointV1 | None:
    """Revalidate a table endpoint's complete grain through the merged authority resolver.

    Returns a ``GovernedEndpointV1`` iff a servable grain exists and all short grain columns are real
    columns of ``table_ref``. ``grain_fact_key`` remains stable while ``grain_fact_revision`` pins
    the exact overlay confirmation or catalog content read.
    """
    ref = _table_object_ref(catalog, table_ref)
    grain = resolve_fact(conn, adapter, ref, "grain", now=now)
    # Fail closed: resolve_fact serves only catalog-authoritative or VERIFIED overlay values.
    if grain.value is None:
        return None
    if not isinstance(grain.value, Mapping):
        return None
    columns = grain.value.get("columns")
    is_unique = grain.value.get("is_unique")
    if (
        not isinstance(columns, list)
        or not columns
        or not all(isinstance(column, str) and column.strip() for column in columns)
        or not isinstance(is_unique, bool)
    ):
        return None
    # Membership: each short grain column must be a real column of THIS table in graph_node
    # (validated against column_name, scoped to catalog + table). A grain column the physical graph
    # lacks means the endpoint is untrustworthy -> fail closed.
    schema, _, table = table_ref.rpartition(_SCHEMA_SEP)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM graph_node "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
            (catalog, table))
        real_columns = {row[0] for row in cur.fetchall()}
    qualified: list[str] = []
    for col in columns:
        if col not in real_columns:
            return None
        qualified.append(f"{table_ref}{_SCHEMA_SEP}{col}")
    key = fact_key(ref, "grain")
    authority_revision = _authority_and_revision(
        conn, resolved=grain, grain_fact_key=key)
    if authority_revision is None:
        return None
    authority, revision = authority_revision
    dependency_identity = canonical_hash({
        "grain_fact_key": key,
        "grain_fact_revision": revision,
        "grain_key_refs": qualified,
        "grain_is_unique": is_unique,
        "grain_authority_provenance": authority.value,
    })
    return GovernedEndpointV1(
        catalog=catalog, table_ref=table_ref, grain_key_refs=tuple(qualified),
        grain_fact_key=key, grain_is_unique=is_unique,
        grain_authority_provenance=authority,
        grain_fact_revision=revision,
        grain_dependency_identity=dependency_identity,
    )
