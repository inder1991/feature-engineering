"""Phase-3B.3a A5 — the authorization/read-scope resolver. The ONE place role logic + watermark stamping
happen; produces a FROZEN CatalogScopeV1 the planner core treats as immutable input."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime

from featuregen.overlay.catalog_changes import drift_head_seq, drift_watermark
from featuregen.overlay.upload.planner.contracts import (
    MAX_AUTHORIZED_CATALOGS_CONSIDERED,
    READ_SCOPE_POLICY_VERSION,
    ROLE_RESOLUTION_VERSION,
    CatalogOmissionReason,
    CatalogScopeV1,
    CatalogStateStampV1,
    OmittedCatalogV1,
)
from featuregen.overlay.upload.read_scope import allowed_sensitivities


def resolve_catalog_scope(conn, *, roles: Iterable[str] = (), target_entity: str | None,
                          now: datetime, requested_sources: tuple[str, ...] | None = None) -> CatalogScopeV1:
    rows = conn.execute(
        "SELECT DISTINCT catalog_source FROM graph_node WHERE kind = 'column' "
        "AND (sensitivity IS NULL OR sensitivity = ANY(%s)) ORDER BY catalog_source",
        (allowed_sensitivities(roles),)).fetchall()
    readable = [r[0] for r in rows]
    if requested_sources is not None:
        readable = [s for s in readable if s in set(requested_sources)]

    authorized: list[str] = []
    stamps: list[CatalogStateStampV1] = []
    omitted: list[OmittedCatalogV1] = []
    for src in readable:
        wm = drift_watermark(conn, src)
        head = drift_head_seq(conn, src)
        if wm is None:
            omitted.append(OmittedCatalogV1(src, CatalogOmissionReason.no_usable_state_stamp))
            continue
        authorized.append(src)
        stamps.append(CatalogStateStampV1(catalog_source=src, head_seq=head or 0,
                                          last_completed_at=wm.isoformat()))
    truncated = len(authorized) > MAX_AUTHORIZED_CATALOGS_CONSIDERED
    if truncated:
        for src in authorized[MAX_AUTHORIZED_CATALOGS_CONSIDERED:]:
            omitted.append(OmittedCatalogV1(src, CatalogOmissionReason.catalog_consideration_bound))
        authorized = authorized[:MAX_AUTHORIZED_CATALOGS_CONSIDERED]
        stamps = stamps[:MAX_AUTHORIZED_CATALOGS_CONSIDERED]

    material = "|".join(f"{s.catalog_source}:{s.head_seq}:{s.last_completed_at}" for s in stamps)
    material += f"|{READ_SCOPE_POLICY_VERSION}|{ROLE_RESOLUTION_VERSION}|{target_entity or ''}"
    scope_id = "cs_" + hashlib.sha256(material.encode()).hexdigest()[:16]
    return CatalogScopeV1(
        scope_id=scope_id, authorized_catalog_sources=tuple(authorized),
        catalog_state_stamps=tuple(stamps), omitted_catalog_sources=tuple(omitted),
        read_scope_policy_version=READ_SCOPE_POLICY_VERSION, role_resolution_version=ROLE_RESOLUTION_VERSION,
        resolved_at=now.isoformat(), catalog_consideration_truncated=truncated)
