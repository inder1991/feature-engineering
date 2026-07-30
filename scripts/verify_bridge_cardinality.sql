\set ON_ERROR_STOP on

-- Task 0: read-only bridge/cardinality baseline. This script reports metadata and
-- aggregate control-plane counts only; it never reads business/source rows.
BEGIN TRANSACTION READ ONLY;

SELECT 'migration_head' AS metric, max(name)::text AS value
FROM schema_migrations;

SELECT 'pass_c_candidate_rows' AS metric, count(*)::text AS value
FROM pass_c_candidate_evidence
UNION ALL
SELECT 'graph_join_edges', count(*)::text
FROM graph_edge
WHERE kind = 'joins'
UNION ALL
SELECT 'cross_catalog_bridge_candidates', count(*)::text
FROM entity_bridge_candidate_evidence
UNION ALL
SELECT 'bridge_candidates_with_cardinality', count(*)::text
FROM entity_bridge_candidate_evidence
WHERE evidence_json ?| ARRAY[
    'cardinality',
    'declared_cardinality',
    'inferred_cardinality',
    'cardinality_status'
]
UNION ALL
SELECT 'projected_entity_bridge_edges', count(*)::text
FROM entity_bridge_edge;

SELECT
    'catalog_tables.' || catalog_source AS metric,
    count(DISTINCT table_name)::text AS value
FROM graph_node
WHERE kind = 'table'
GROUP BY catalog_source
ORDER BY catalog_source;

-- Fold the entity_bridge event streams using the same status transitions as
-- featuregen.overlay.state.fold_overlay_state. In particular:
--   * a stray PROPOSED cannot regress an active fact to DRAFT;
--   * EXPIRED folds to REVERIFY, not to an "expired" status.
WITH RECURSIVE
bridge_facts AS (
    SELECT DISTINCT overlay_fact_id AS fact_key
    FROM events
    WHERE aggregate = 'overlay_fact'
      AND type = 'OVERLAY_FACT_PROPOSED'
      AND payload->>'fact_type' = 'entity_bridge'
      AND overlay_fact_id IS NOT NULL
),
ordered AS (
    SELECT
        e.overlay_fact_id AS fact_key,
        row_number() OVER (
            PARTITION BY e.overlay_fact_id
            ORDER BY e.stream_version
        ) AS rn,
        e.type
    FROM events e
    JOIN bridge_facts b ON b.fact_key = e.overlay_fact_id
    WHERE e.aggregate = 'overlay_fact'
),
folded AS (
    SELECT fact_key, 0::bigint AS rn, NULL::text AS status
    FROM bridge_facts

    UNION ALL

    SELECT
        f.fact_key,
        e.rn,
        CASE e.type
            WHEN 'OVERLAY_FACT_PROPOSED' THEN
                CASE
                    WHEN f.status IS NULL OR f.status = 'REJECTED' THEN 'DRAFT'
                    ELSE f.status
                END
            WHEN 'OVERLAY_FACT_PARTIALLY_CONFIRMED' THEN 'PARTIALLY_CONFIRMED'
            WHEN 'OVERLAY_FACT_CONFIRMED' THEN 'VERIFIED'
            WHEN 'OVERLAY_FACT_REJECTED' THEN 'REJECTED'
            WHEN 'OVERLAY_FACT_EXPIRED' THEN 'REVERIFY'
            WHEN 'OVERLAY_FACT_STALED' THEN 'STALE'
            ELSE f.status
        END
    FROM folded f
    JOIN ordered e
      ON e.fact_key = f.fact_key
     AND e.rn = f.rn + 1
),
heads AS (
    SELECT DISTINCT ON (fact_key) fact_key, status
    FROM folded
    ORDER BY fact_key, rn DESC
)
SELECT
    'bridge_lifecycle.' || coalesce(status, 'MISSING') AS metric,
    count(*)::text AS value
FROM heads
GROUP BY status
ORDER BY status;

WITH bridge_facts AS (
    SELECT DISTINCT overlay_fact_id AS fact_key
    FROM events
    WHERE aggregate = 'overlay_fact'
      AND type = 'OVERLAY_FACT_PROPOSED'
      AND payload->>'fact_type' = 'entity_bridge'
      AND overlay_fact_id IS NOT NULL
)
SELECT 'open_bridge_human_tasks' AS metric, count(*)::text AS value
FROM human_tasks h
JOIN bridge_facts b ON b.fact_key = h.fact_key
WHERE h.status = 'open';

ROLLBACK;
