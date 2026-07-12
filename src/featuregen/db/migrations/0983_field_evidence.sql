-- src/featuregen/db/migrations/0983_field_evidence.sql
-- Spec §5.1: the per-field proposal store. Every Phase-1 producer (glossary reader, sample-value
-- parser, Pass A LLM, taxonomy) writes one immutable row here PER (object-field, proposed value);
-- the resolver reads the ACTIVE set for a field by (logical_ref, field_name). Append-only and
-- lifecycle-gated: a source re-upload STALEs its own superseded rows (producer-scoped — see
-- field_evidence.stale_source_evidence) rather than deleting them, and NEVER touches human /
-- taxonomy evidence. `logical_ref` is the shared, schema-preserving object identity minted by
-- overlay.upload.object_ref.normalize_ref (the same key field_decision and, later, graph_node use).
-- `proposed_value` is the raw jsonb proposal; `proposed_value_hash` is its order-independent
-- canonical hash. `input_hash` is the per-FIELD input hash that keys staleness across snapshots
-- (unchanged input -> not re-written / not re-staled). Idempotent (IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS field_evidence (
    evidence_id                 text        PRIMARY KEY,
    logical_ref                 text        NOT NULL,
    field_name                  text        NOT NULL,
    proposed_value              jsonb       NOT NULL,
    proposed_value_hash         text        NOT NULL,
    producer                    text        NOT NULL,   -- source|structural_connector|parser|llm|profiler|taxonomy|human|legacy
    strength                    text        NOT NULL,   -- proposed|supported|attested|confirmed
    lifecycle                   text        NOT NULL DEFAULT 'active',  -- active|stale|rejected|superseded
    producer_ref                text        NOT NULL,
    producer_item_ref           text        NULL,
    producer_configuration_hash text        NULL,
    evidence_spans              jsonb       NOT NULL DEFAULT '[]',
    confidence_band             text        NULL,
    source_snapshot_id          text        NOT NULL,
    input_hash                  text        NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS field_evidence_object_idx
    ON field_evidence (logical_ref, field_name, lifecycle);
