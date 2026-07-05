"""Phase-2 feature layer — deterministic spine.

Features enter the catalog here (the "feature source" S1 gated the phase-2 assist on). A feature
records the columns it derives from; from those derives-from links we compute, deterministically:
freshness lineage (a feature is only as fresh as its stalest source) and drift impact (which features
break when a column drifts). The LLM-assist (recommendation, NL->recipe, leakage) layers on top.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.catalog_changes import drift_watermark


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    description: str = ""
    grain_table: str | None = None
    aggregation: str | None = None
    as_of_column: str | None = None
    # the source columns the feature reads: (catalog_source, object_ref)
    derives_from: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class FeatureFreshness:
    fresh: bool
    stale_sources: list[str] = field(default_factory=list)


def register_feature(conn, spec: FeatureSpec) -> str:
    feature_id = mint_id("feat")
    conn.execute(
        "INSERT INTO feature (feature_id, name, description, grain_table, aggregation, as_of_column) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (feature_id, spec.name, spec.description, spec.grain_table, spec.aggregation,
         spec.as_of_column))
    for catalog_source, object_ref in spec.derives_from:
        conn.execute(
            "INSERT INTO feature_derives_from (feature_id, catalog_source, object_ref) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (feature_id, catalog_source, object_ref))
    return feature_id


def features_affected_by(conn, catalog_source: str, object_ref: str) -> list[str]:
    """Drift impact: the features that derive from a given (source, column). Reverse traversal."""
    rows = conn.execute(
        "SELECT feature_id FROM feature_derives_from "
        "WHERE catalog_source = %s AND object_ref = %s ORDER BY feature_id",
        (catalog_source, object_ref)).fetchall()
    return [r[0] for r in rows]


def feature_freshness(conn, feature_id: str, *, now: datetime,
                      fresh_within: timedelta = timedelta(hours=24)) -> FeatureFreshness:
    """A feature is fresh only if EVERY source it derives from is fresh (min over sources)."""
    sources = {r[0] for r in conn.execute(
        "SELECT DISTINCT catalog_source FROM feature_derives_from WHERE feature_id = %s",
        (feature_id,)).fetchall()}
    cutoff = now - fresh_within
    stale: list[str] = []
    for src in sorted(sources):
        wm = drift_watermark(conn, src)
        if wm is None or wm < cutoff:
            stale.append(src)
    return FeatureFreshness(fresh=not stale, stale_sources=stale)
