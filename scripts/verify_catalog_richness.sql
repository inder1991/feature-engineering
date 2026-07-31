-- scripts/verify_catalog_richness.sql
-- Ingestion-richness plan, Task 0: reproduce the metadata audit as metric|value rows.
-- READ-ONLY. Run: psql -tA -f scripts/verify_catalog_richness.sql
-- Baseline reference: plan "Verified Audit Baseline" (2026-07-31). Any drift updates the plan
-- BEFORE other tasks start; after Task 6 this same script asserts the target numbers.

-- ── inventory + per-axis coverage, per catalog ──────────────────────────────────────────────
SELECT 'columns|' || catalog_source || '|' || count(*) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_concept|' || catalog_source || '|' || count(concept) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_definition|' || catalog_source || '|' || count(definition) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_domain|' || catalog_source || '|' || count(domain) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_entity_display|' || catalog_source || '|' || count(NULLIF(entity,'')) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_sensitivity_display|' || catalog_source || '|' || count(sensitivity) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_attested_type|' || catalog_source || '|' || count(NULLIF(data_type,'unknown')) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_declared_type|' || catalog_source || '|' || count(declared_type) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_unit|' || catalog_source || '|' || count(unit) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_currency|' || catalog_source || '|' || count(currency) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_additivity|' || catalog_source || '|' || count(additivity) FROM graph_node WHERE kind='column' GROUP BY catalog_source
UNION ALL
SELECT 'coverage_ai_summary|' || catalog_source || '|' || count(ai_summary) FROM graph_node WHERE kind='column' GROUP BY catalog_source
-- (coverage_party_role joins this script when Task 3's migration adds the column —
--  a parse-time column reference cannot be guarded by an EXISTS subquery)

-- ── read-scope enforcement distribution (display axis is separate; this is the ENFORCED one) ─
UNION ALL
SELECT 'visible_requires|' || coalesce(visible_requires::text,'NULL') || '|' || count(*)
FROM graph_node WHERE kind='column' GROUP BY visible_requires

-- ── governed facts vs table-node stamps (Task 5's reconcile) ────────────────────────────────
UNION ALL
SELECT 'overlay_fact|' || fact_type || ':' || status || '|' || count(*)
FROM overlay_fact_state GROUP BY fact_type, status
UNION ALL
SELECT 'stamp_drift|' || op.object_ref || '|' || fs.fact_type
FROM overlay_fact_state fs
JOIN overlay_proposal op USING (fact_key)
LEFT JOIN graph_node t ON t.kind='table' AND op.object_ref = 'public.' || t.table_name
WHERE fs.status='VERIFIED' AND fs.fact_type IN ('grain','availability_time')
  AND ((fs.fact_type='grain' AND t.grain_fact_event_id IS NULL)
    OR (fs.fact_type='availability_time' AND t.availability_fact_event_id IS NULL))

-- ── semantic-binding pipeline (Task 4) ──────────────────────────────────────────────────────
UNION ALL
SELECT 'binding_candidates||' || count(*) FROM semantic_binding_candidate
UNION ALL
SELECT 'binding_proposals||' || count(*) FROM semantic_binding_candidate_proposal
UNION ALL
SELECT 'binding_edges||' || count(*) FROM semantic_binding_edge

-- ── bridge candidates + governance load ─────────────────────────────────────────────────────
UNION ALL
SELECT 'bridge_candidates_by_entity|' || entity_id || '|' || count(*)
FROM entity_bridge_candidate_evidence GROUP BY entity_id
UNION ALL
SELECT 'bridge_candidates_total||' || count(*) FROM entity_bridge_candidate_evidence
UNION ALL
SELECT 'open_human_tasks||' || count(*) FROM human_tasks WHERE status='open'

-- ── concept-conflation listing (WRONG-axis witnesses; empty after Task 6 except residue) ────
UNION ALL
SELECT 'conflation|' || concept || '|' || string_agg(catalog_source || '.' || column_name, ',' ORDER BY catalog_source, column_name)
FROM graph_node
WHERE kind='column' AND concept IN ('counterparty_id','branch_id','account_id','transaction_id','pii','category_code')
GROUP BY concept

-- ── migration ledger: both 1036-1039 sets, by name (merge reconciliation witness) ───────────
UNION ALL
SELECT 'migration|' || name || '|applied' FROM schema_migrations
WHERE name ~ '^103[6-9]' ORDER BY 1;
