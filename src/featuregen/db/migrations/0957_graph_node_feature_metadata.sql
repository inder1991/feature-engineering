-- src/featuregen/db/migrations/0957_graph_node_feature_metadata.sql
-- Feature-correctness metadata on a column node: additivity (whether you may SUM it, and over which
-- dimensions), unit/currency (scale — dollars vs cents), and the business entity it denotes. These
-- surface on search results so a feature-builder aggregates correctly (a wrong unit or summing a
-- non-additive balance over time is a silently-wrong feature).
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS additivity text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS unit       text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS currency   text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS entity     text NULL;
