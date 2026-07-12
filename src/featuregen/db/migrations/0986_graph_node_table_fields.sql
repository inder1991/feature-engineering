-- Phase 2 (table facts): advisory table-level fields (display-only, RECOMMENDATION-ceilinged) and
-- the grain/as-of specialized-fact provenance link populated by the projection bridge (Task 9).
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS table_role text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS primary_entity text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS event_or_snapshot text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS table_role_decision_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS primary_entity_decision_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS event_or_snapshot_decision_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS grain_fact_event_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS availability_fact_event_id text;
