-- src/featuregen/db/migrations/1015_semantic_binding_projection.sql
-- Delivery E (E3) — the OPERATIONAL projection of a VERIFIED governed semantic fact.
--
-- E1 registered the fact types (entity_assignment / currency_binding); D1/D2 propose DRAFT
-- candidates. This migration is the durable OPERATIONAL surface a CONFIRMED (VERIFIED) semantic fact
-- projects onto:
--   * entity_assignment  -> graph_node.entity (the effective business entity) + provenance links +
--     the source file's declared entity PRESERVED as labelled context (declared_entity), so a
--     demotion RESTORES the file display without data loss.
--   * currency_binding   -> a `semantic_binding_edge` row (measure column -> currency column).
--
-- Additive + idempotent (the repo style: ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS /
-- CREATE INDEX IF NOT EXISTS), so apply_migrations stays re-runnable and the test suite can re-apply
-- this exact SQL. Highest deployed before this = 1014; this is 1015.
--
-- `semantic_binding_edge` is a PROJECTION table (MUTABLE — rebuilt from the overlay_fact event
-- stream by the registered SemanticBindingProjection; NOT WORM). The load-bearing truth is the fact
-- stream; this row is its operational read model. Its `status` is the fact's folded lifecycle status
-- and operational readers require status='VERIFIED' as a SECOND fail-closed gate (a stale/demoted
-- projection can never serve a non-VERIFIED binding).

-- =====================================================================================================
-- 1) semantic_binding_edge — the currency_binding operational projection (measure -> currency column).
--    fact_key PRIMARY KEY: one edge per governed fact (the projector upserts by fact_key). `kind` is
--    the E1 fact type it projects (currency_binding today; entity_assignment rides graph_node, below).
-- =====================================================================================================
CREATE TABLE IF NOT EXISTS semantic_binding_edge (
    fact_key            text        PRIMARY KEY,
    catalog_source      text        NOT NULL,
    kind                text        NOT NULL CHECK (kind IN ('currency_binding')),
    from_ref            text        NOT NULL,   -- the subject measure column (public graph scope)
    to_ref              text        NOT NULL,   -- the target currency column (public graph scope)
    confirmed_event_id  text        NULL,       -- the OVERLAY_FACT_CONFIRMED event that governs it
    -- The fact's folded lifecycle status. Operational readers require 'VERIFIED' (the SECOND gate);
    -- a demotion stamps the non-VERIFIED folded status and KEEPS the row (audit trail).
    status              text        NOT NULL DEFAULT 'VERIFIED'
        CHECK (status IN ('DRAFT', 'PARTIALLY_CONFIRMED', 'VERIFIED', 'REJECTED', 'STALE', 'REVERIFY')),
    projected_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS semantic_binding_edge_from_idx   ON semantic_binding_edge (from_ref);
CREATE INDEX IF NOT EXISTS semantic_binding_edge_to_idx     ON semantic_binding_edge (to_ref);
CREATE INDEX IF NOT EXISTS semantic_binding_edge_status_idx ON semantic_binding_edge (status);

-- =====================================================================================================
-- 2) graph_node — the entity_assignment operational projection lands here (a column node's effective
--    `entity` already exists, 0957). ADD the governed-projection columns:
--      * declared_entity     — the source file's display entity, PRESERVED as labelled context so a
--        demotion restores it (never data loss). Also the durable DIVERGENCE signal: when a re-upload
--        declares an entity that DIFFERS from the governed one, declared_entity holds the file's value
--        while `entity` holds the governed (VERIFIED-wins) value — declared_entity <> entity records
--        the conflict WITHOUT overwriting the governed binding.
--      * entity_fact_key / entity_fact_event_id — provenance links to the governing entity_assignment
--        fact + its confirming event (the demotion locates the node by entity_fact_key).
--      * entity_status       — 'VERIFIED' while the governed binding is operational; NULL once demoted
--        (the SECOND fail-closed gate for operational entity reads).
-- Additive/nullable: a technical/declared upload with no governed entity is byte-for-byte unchanged.
-- =====================================================================================================
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS declared_entity     text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS entity_fact_key     text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS entity_fact_event_id text NULL;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS entity_status       text NULL
    CHECK (entity_status IS NULL OR entity_status = 'VERIFIED');
CREATE INDEX IF NOT EXISTS graph_node_entity_fact_key_idx
    ON graph_node (entity_fact_key) WHERE entity_fact_key IS NOT NULL;
