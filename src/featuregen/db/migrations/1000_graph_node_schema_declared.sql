-- src/featuregen/db/migrations/1000_graph_node_schema_declared.sql
-- (renumbered 0999->1000: origin/main took 0999_planner_shadow_store.sql via Phase 3B.4)
-- FTR glossary adapter A1 (Tasks 5+8) — three ADDITIVE, nullable graph_node columns. The
-- operational object identity stays the public-flattened ref (single-schema until Delivery C);
-- these columns preserve what the flatten would otherwise discard, without changing any key:
--   schema_name   — the REAL (pre-flatten) schema the upload declared for this table/column, raw
--                   case as declared. Written by build_graph on BOTH table and column nodes
--                   (round-4 #5 — a graph column, NOT projectable by resolve_and_project). NULL =
--                   no schema was ever attested (technical/generic uploads) — the cross-schema
--                   fence treats NULL as UNVERIFIABLE and holds rather than letting a new schema
--                   silently claim the identity (round-4 #4 legacy-NULL policy).
--   declared_type — the FTR-declared SQL type, bounded + validated by the adapter, retained as
--                   NON-operational metadata (round-4 #1: the operational data_type stays
--                   UNKNOWN_TYPE — a business glossary is not the physical-type authority).
--   semantic_terms — search-only glossary semantics (term/synonyms/BIAN/FIBO/process), populated
--                   by Task 8; added here so both tasks share one migration.
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS schema_name text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS declared_type text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS semantic_terms text NULL;
