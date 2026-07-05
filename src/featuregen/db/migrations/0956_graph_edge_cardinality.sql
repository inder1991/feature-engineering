-- src/featuregen/db/migrations/0956_graph_edge_cardinality.sql
-- Join edges: cardinality on a graph_edge (N:1 | 1:1 | 1:N), used by feature-building to know whether
-- a join fans in safely (N:1) or would double-count. A 'joins' edge may point at a column that isn't
-- loaded yet (a cross-source / pending join) — the edge is still recorded.
ALTER TABLE graph_edge ADD COLUMN IF NOT EXISTS cardinality text NULL;
