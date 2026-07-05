-- src/featuregen/db/migrations/0954_graph_node_sensitivity.sql
-- Read-scope: the declared sensitivity of a column (pii | restricted | ...), used as a HARD
-- pre-filter in search so the estate's PII map is not world-readable. A sensitivity-tagged node is
-- returned only to a caller whose roles grant that sensitivity.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS sensitivity text NULL;
