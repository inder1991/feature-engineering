-- src/featuregen/db/migrations/0985_field_revalidation.sql
-- Spec §6.3 (review must-fix #4): the human-confirmation revalidation store. When a source re-upload
-- changes a column's MATERIAL (its definition/type) AND a human-confirmed decision/evidence already
-- exists for one of its fields, the human evidence is NEVER staled (a source re-upload must not stale
-- human evidence) — instead the field is flagged PENDING revalidation here. That pending flag surfaces
-- as overlay.field_authority.Disqualifier.CONFIRMATION_PENDING_REVALIDATION, which the field policy
-- honours, so the resolver BLOCKS the load-bearing value until a human re-confirms. `logical_ref` is
-- the shared, schema-preserving object identity (overlay.upload.object_ref.normalize_ref) the rest of
-- the per-field stores key on; `source_snapshot_id` ties the flag to the ingestion run that raised it.
-- Idempotent (IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS field_revalidation (
    revalidation_id     text        PRIMARY KEY,
    logical_ref         text        NOT NULL,
    field_name          text        NOT NULL,
    reason              text        NOT NULL,
    source_snapshot_id  text        NOT NULL,
    status              text        NOT NULL DEFAULT 'pending',   -- pending|cleared
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS field_revalidation_pending_idx
    ON field_revalidation (logical_ref, field_name, status);
