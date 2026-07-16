-- src/featuregen/db/migrations/0993_graph_check_constraints.sql
-- Round-3 #19: PostgreSQL enforces the graph's enum-like invariants. graph_node/graph_edge stored
-- their closed vocabularies as unrestricted text, so an application bug could persist malformed
-- operational state (e.g. an edge whose authority/approved_join_status typo silently escapes the
-- governed-join filters). Each CHECK below matches EXACTLY the values the application writes:
--   graph_node.kind             — build_graph/add_column_row (graph.py)
--   graph_node.sensitivity      — canonical._VALID_SENSITIVITY ('' -> NULL) = read_scope.SENSITIVITY_ROLES
--   graph_edge.kind             — graph.py ('contains'/'joins') + passc/projection.py ('joins')
--   graph_edge.cardinality      — canonical._VALID_CARDINALITY / the approved_join value schema
--   graph_edge.authority        — graph.py + passc/projection.py (0982 default 'operational')
--   graph_edge.approved_join_status — the canonical folded FactStatus vocabulary (overlay/state.py):
--       the projector writes 'VERIFIED'/NULL and demote_join_edges stamps the fact's folded status
--       ('REJECTED'/'REVERIFY'/'STALE' today) — the full vocabulary is admitted as the superset.
-- Deliberately NO foreign keys (a 'joins' edge may reference a not-yet-loaded endpoint — documented
-- design choice) and NO constraints on free-text columns (definition, concept, domain, ...).
-- Idempotent via DROP CONSTRAINT IF EXISTS + ADD (the 0504/0505/0506 pattern).

ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_kind_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_kind_check
    CHECK (kind IN ('table', 'column'));

ALTER TABLE graph_node DROP CONSTRAINT IF EXISTS graph_node_sensitivity_check;
ALTER TABLE graph_node ADD CONSTRAINT graph_node_sensitivity_check
    CHECK (sensitivity IS NULL OR sensitivity IN ('pii', 'restricted'));

ALTER TABLE graph_edge DROP CONSTRAINT IF EXISTS graph_edge_kind_check;
ALTER TABLE graph_edge ADD CONSTRAINT graph_edge_kind_check
    CHECK (kind IN ('contains', 'joins'));

ALTER TABLE graph_edge DROP CONSTRAINT IF EXISTS graph_edge_cardinality_check;
ALTER TABLE graph_edge ADD CONSTRAINT graph_edge_cardinality_check
    CHECK (cardinality IS NULL OR cardinality IN ('1:1', '1:N', 'N:1'));

ALTER TABLE graph_edge DROP CONSTRAINT IF EXISTS graph_edge_authority_check;
ALTER TABLE graph_edge ADD CONSTRAINT graph_edge_authority_check
    CHECK (authority IN ('operational', 'display_only'));

ALTER TABLE graph_edge DROP CONSTRAINT IF EXISTS graph_edge_approved_join_status_check;
ALTER TABLE graph_edge ADD CONSTRAINT graph_edge_approved_join_status_check
    CHECK (approved_join_status IS NULL OR approved_join_status IN
           ('DRAFT', 'PARTIALLY_CONFIRMED', 'VERIFIED', 'REJECTED', 'STALE', 'REVERIFY'));
