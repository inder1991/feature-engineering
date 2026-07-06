"""Phase-2 feature layer — deterministic spine.

Features enter the catalog here (the "feature source" S1 gated the phase-2 assist on). A feature
records the columns it derives from; from those derives-from links we compute, deterministically:
freshness lineage (a feature is only as fresh as its stalest source) and drift impact (which features
break when a column drifts). The LLM-assist (recommendation, NL->recipe, leakage) layers on top.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.upload.read_scope import allowed_sensitivities


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    description: str = ""
    grain_table: str | None = None
    aggregation: str | None = None
    as_of_column: str | None = None
    # the source columns the feature reads: (catalog_source, object_ref)
    derives_from: tuple[tuple[str, str], ...] = ()
    verification: str = "DESIGN-CHECKED"   # §14.5 honest stamp, persisted on the row (0968)


@dataclass(frozen=True, slots=True)
class FeatureFreshness:
    fresh: bool
    stale_sources: list[str] = field(default_factory=list)


def register_feature(conn, spec: FeatureSpec) -> str:
    feature_id = mint_id("feat")
    conn.execute(
        "INSERT INTO feature (feature_id, name, description, grain_table, aggregation, as_of_column, "
        "verification) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (feature_id, spec.name, spec.description, spec.grain_table, spec.aggregation,
         spec.as_of_column, spec.verification))
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


def list_features(conn, *, limit: int = 50) -> list[dict]:
    """The registered-feature inventory (registry READ surface — the catalog was write-only)."""
    rows = conn.execute(
        "SELECT feature_id, name, grain_table, aggregation, as_of_column, verification, created_at "
        "FROM feature ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return [{"feature_id": r[0], "name": r[1], "grain_table": r[2], "aggregation": r[3],
             "as_of_column": r[4], "verification": r[5], "created_at": r[6].isoformat()} for r in rows]


def get_feature(conn, feature_id: str, *, roles: Iterable[str] = ()) -> dict | None:
    """One registered feature + the source columns it derives from. Lineage is READ-SCOPED: a derives
    column whose sensitivity the caller's roles can't see is withheld (same control /search enforces),
    so the registry can't be used to enumerate where restricted/PII columns live."""
    row = conn.execute(
        "SELECT feature_id, name, description, grain_table, aggregation, as_of_column, verification, "
        "created_at FROM feature WHERE feature_id = %s", (feature_id,)).fetchone()
    if row is None:
        return None
    derives = conn.execute(
        "SELECT d.catalog_source, d.object_ref FROM feature_derives_from d "
        "LEFT JOIN graph_node n ON n.catalog_source = d.catalog_source AND n.object_ref = d.object_ref "
        "WHERE d.feature_id = %s AND (n.sensitivity IS NULL OR n.sensitivity = ANY(%s)) "
        "ORDER BY d.object_ref", (feature_id, allowed_sensitivities(roles))).fetchall()
    return {"feature_id": row[0], "name": row[1], "description": row[2], "grain_table": row[3],
            "aggregation": row[4], "as_of_column": row[5], "verification": row[6],
            "created_at": row[7].isoformat(),
            "derives_from": [{"catalog_source": d[0], "object_ref": d[1]} for d in derives]}


def register_consumer(conn, *, model_ref: str, feature_id: str, purpose: str = "",
                      environment: str = "dev", actor: str = "") -> str | None:
    """Register a model/consumer as a user of a feature (SP-14). Idempotent per (model, feature, env).
    Returns the consumer_id, or None if the feature doesn't exist (checked first — no FK abort)."""
    if conn.execute("SELECT 1 FROM feature WHERE feature_id = %s", (feature_id,)).fetchone() is None:
        return None
    row = conn.execute(
        "INSERT INTO feature_consumer (consumer_id, model_ref, feature_id, purpose, environment, actor) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (model_ref, feature_id, environment) DO UPDATE SET purpose = EXCLUDED.purpose, "
        "actor = EXCLUDED.actor RETURNING consumer_id",
        (mint_id("cons"), model_ref, feature_id, purpose, environment, actor)).fetchone()
    return row[0]


def consumers_of_feature(conn, feature_id: str) -> list[dict]:
    """Which models consume this feature — the change-impact / deprecation-scoping answer."""
    rows = conn.execute(
        "SELECT model_ref, purpose, environment, registered_at FROM feature_consumer "
        "WHERE feature_id = %s ORDER BY model_ref, environment", (feature_id,)).fetchall()
    return [{"model_ref": r[0], "purpose": r[1], "environment": r[2],
             "registered_at": r[3].isoformat()} for r in rows]


def features_for_consumer(conn, model_ref: str) -> list[dict]:
    """Which features a model consumes."""
    rows = conn.execute(
        "SELECT fc.feature_id, f.name, fc.purpose, fc.environment FROM feature_consumer fc "
        "JOIN feature f ON f.feature_id = fc.feature_id WHERE fc.model_ref = %s ORDER BY f.name",
        (model_ref,)).fetchall()
    return [{"feature_id": r[0], "name": r[1], "purpose": r[2], "environment": r[3]} for r in rows]
