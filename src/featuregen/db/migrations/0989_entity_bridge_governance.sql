-- src/featuregen/db/migrations/0989_entity_bridge_governance.sql
-- Phase 3B.2B: governed cross-catalog entity bridges. The bridge LIFECYCLE rides the generic
-- overlay_fact event stream (fact_type='entity_bridge') — these tables are only the durable candidate
-- ledger + the VERIFIED projection. entity_bridge_edge is cross-catalog (graph_edge is intra-catalog-
-- keyed), and is what the 3B.3 planner reads. Additive-only; nothing consumes it until 3B.3.
CREATE TABLE IF NOT EXISTS entity_bridge_candidate_evidence (
    entity_id            text        NOT NULL,
    left_catalog_source  text        NOT NULL,
    left_object_ref      text        NOT NULL,
    right_catalog_source text        NOT NULL,
    right_object_ref     text        NOT NULL,
    candidate_id         text        NOT NULL,
    fact_key             text        NULL,
    proposed_event_id    text        NULL,
    data_type_family     text        NOT NULL,
    evidence_json        jsonb       NOT NULL DEFAULT '{}',
    derivation_version   text        NOT NULL,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref),
    CONSTRAINT entity_bridge_candidate_distinct_sources
        CHECK (left_catalog_source <> right_catalog_source)
);

CREATE TABLE IF NOT EXISTS entity_bridge_edge (
    fact_key             text        PRIMARY KEY,
    entity_id            text        NOT NULL,
    left_catalog_source  text        NOT NULL,
    left_object_ref      text        NOT NULL,
    right_catalog_source text        NOT NULL,
    right_object_ref     text        NOT NULL,
    confirmed_event_id   text        NULL,
    status               text        NOT NULL,   -- 'VERIFIED'
    projected_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entity_bridge_edge_entity_idx ON entity_bridge_edge (entity_id);
