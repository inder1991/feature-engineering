-- src/featuregen/db/migrations/0951_graph_node_concept.sql
-- Enrichment: the classified concept on a column node (advisory; drives the search sem signal).
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS concept text NULL;
