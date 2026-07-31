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
SELECT 'active_cross_catalog_bridge_candidates', count(*)::text
FROM entity_bridge_candidate_evidence e
JOIN governed_candidate_current c USING (candidate_id)
WHERE c.lifecycle = 'active'
UNION ALL
SELECT 'withdrawn_cross_catalog_bridge_candidates', count(*)::text
FROM governed_candidate_current
WHERE lifecycle = 'withdrawn'
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
FROM entity_bridge_edge
UNION ALL
SELECT 'modern_current_bridge_assessments', count(*)::text
FROM governed_candidate_current c
JOIN governed_candidate_revision r
  ON r.candidate_revision_id = c.candidate_revision_id
WHERE c.lifecycle = 'active'
  AND coalesce(r.assessment_json->>'legacy', 'false') <> 'true'
UNION ALL
SELECT 'current_directional_realizations', count(*)::text
FROM bridge_join_realization_current
UNION ALL
SELECT 'current_relationship_observations', count(*)::text
FROM relationship_observation_current
UNION ALL
SELECT 'physical_binding_revisions', count(*)::text
FROM physical_dataset_binding_revision;

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

-- A stale projection must never make an unavailable symmetric link look endorsed.
-- This repeats the fold instead of trusting entity_bridge_edge.status.
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
SELECT 'unavailable_links_left_in_review_projection' AS metric, count(*)::text AS value
FROM entity_bridge_edge p
LEFT JOIN heads h ON h.fact_key = p.fact_key
WHERE coalesce(h.status, 'MISSING') NOT IN ('DRAFT', 'PARTIALLY_CONFIRMED', 'VERIFIED');

SELECT
    'current_realization_safety.' || safety_status AS metric,
    count(*)::text AS value
FROM bridge_join_realization_current
GROUP BY safety_status
UNION ALL
SELECT
    'current_realization_review.' || review_status,
    count(*)::text
FROM bridge_join_realization_current
GROUP BY review_status
UNION ALL
SELECT
    'current_realization_lifecycle.' || lifecycle,
    count(*)::text
FROM bridge_join_realization_current
GROUP BY lifecycle
ORDER BY metric;

-- These are the pilot's Customer endpoint names. The query reports the symmetric
-- identifier candidates only; it does not imply a directional join cardinality.
SELECT
    candidate_id,
    fact_key,
    left_catalog_source,
    left_object_ref,
    right_catalog_source,
    right_object_ref,
    data_type_family,
    evidence_json->>'type_basis' AS type_basis
FROM entity_bridge_candidate_evidence
JOIN governed_candidate_current USING (candidate_id)
WHERE (
    lower(left_object_ref) LIKE '%.cust_num'
    AND lower(right_object_ref) LIKE '%.cif_id'
) OR (
    lower(left_object_ref) LIKE '%.cif_id'
    AND lower(right_object_ref) LIKE '%.cust_num'
)
AND governed_candidate_current.lifecycle = 'active'
ORDER BY candidate_id;

-- Reconcile the latest completed entity-bridge stage per pilot source. Pass C
-- remains a separate stage and therefore cannot be mistaken for these counts.
WITH latest_completed AS (
    SELECT DISTINCT ON (r.catalog_source)
        r.catalog_source,
        r.id,
        r.completed_at
    FROM ingestion_run r
    WHERE r.catalog_source IN ('cib', 'ftr')
      AND r.status = 'ingested'
    ORDER BY r.catalog_source, r.completed_at DESC
)
SELECT
    r.catalog_source,
    s.state,
    s.reason_code,
    s.detail->>'block_matched_count' AS block_matched_count,
    s.detail->>'considered_count' AS assessments_completed,
    s.detail->>'retained_count' AS retained_count,
    s.detail->>'suppressed_count' AS suppressed_count,
    s.detail->>'truncated_pair_count' AS truncated_pair_count,
    s.detail->>'proposed' AS proposed_count,
    s.detail->>'withdrawn_count' AS withdrawn_count,
    s.detail->>'withdrawn_realization_count' AS withdrawn_realization_count
FROM latest_completed r
LEFT JOIN ingestion_run_stage s
  ON s.ingestion_run_id = r.id
 AND s.stage = 'entity_bridges'
ORDER BY r.catalog_source;

ROLLBACK;
