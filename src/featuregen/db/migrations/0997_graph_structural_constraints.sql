-- src/featuregen/db/migrations/0997_graph_structural_constraints.sql
-- #21: 0993 constrained the graph's closed vocabularies but NOT its structure, so an application
-- bug could still persist a structurally corrupt row (a 'table' node carrying a column_name, a
-- column node whose object_ref is table-shaped, an edge endpoint that is not a dotted ref). Each
-- CHECK below states an invariant of what the writers actually produce:
--   graph.py build_graph / add_column_row (the only graph_node writers):
--     * table node:  object_ref 'public.<table>',           column_name NULL, data_type NULL
--     * column node: object_ref 'public.<table>.<column>',  column_name NOT NULL / non-empty
--     * table_name is always the non-empty table
--   graph.py + passc/projection.py (the graph_edge writers): every endpoint ref is a dotted
--     object_ref rendering — 'public.<table>' or 'public.<table>.<column>' — never empty.
-- Ref-shape checks are deliberately WIDENED to ">= N non-empty dot-separated segments" (>= 2 for
-- a table ref, >= 3 for a column ref) rather than exact segment counts or object_ref ==
-- 'public.' || table_name equality: a row written before validation began quarantining dotted
-- table/column names may carry extra segments, and an exact CHECK would make this ALTER fail on
-- that data. Still NO foreign keys (a 'joins' edge may reference a not-yet-loaded endpoint —
-- documented design choice). Idempotent via DROP CONSTRAINT IF EXISTS + ADD (the 0993 pattern).

-- (a) kind-dependent nullability: a column node names its column; a table node has neither a
--     column_name nor a data_type (build_graph writes NULL for both — data_type NULL also feeds
--     the gn-v1 source_fingerprint contract).
ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_kind_shape_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_kind_shape_check
    CHECK ((kind = 'table' AND column_name IS NULL AND data_type IS NULL)
           OR (kind = 'column' AND column_name IS NOT NULL AND column_name <> ''));

ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_table_name_nonempty_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_table_name_nonempty_check
    CHECK (table_name <> '');

-- (b) object_ref shape matches kind: a dotted path of non-empty segments — a table ref has at
--     least schema.table (2 segments), a column ref one more (>= 3). '^[^.]+(\.[^.]+)+$' is
--     "non-empty segments joined by dots, at least two of them".
ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_object_ref_shape_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_object_ref_shape_check
    CHECK (CASE kind
               WHEN 'table'  THEN object_ref ~ '^[^.]+(\.[^.]+)+$'
               WHEN 'column' THEN object_ref ~ '^[^.]+(\.[^.]+){2,}$'
               ELSE true
           END);

-- (c) graph_edge endpoints: every from_ref/to_ref either writer produces is a dotted object_ref
--     rendering with non-empty segments (NOT NULL already holds; '' or an undotted token would
--     be a corrupt edge no traversal could ever bind).
ALTER TABLE graph_edge DROP CONSTRAINT IF EXISTS graph_edge_ref_shape_check;
ALTER TABLE graph_edge ADD CONSTRAINT graph_edge_ref_shape_check
    CHECK (from_ref ~ '^[^.]+(\.[^.]+)+$' AND to_ref ~ '^[^.]+(\.[^.]+)+$');
