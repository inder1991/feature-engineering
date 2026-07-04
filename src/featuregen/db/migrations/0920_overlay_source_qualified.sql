-- src/featuregen/db/migrations/0920_overlay_source_qualified.sql
-- SP-1.5 Task 2: canonical SOURCE-QUALIFIED reference model. Every overlay read-model / snapshot /
-- dependency sink keys on the PAIR (catalog_source, canonical_object_ref) so two catalogs with an
-- identically-named schema.table.column no longer collide across the read surface.
--
-- MAINTENANCE-WINDOW / reset-and-replay migration (plan F7): the read models are droppable +
-- deterministically rebuildable — overlay_fact_state/proposal/dependency from the overlay event
-- stream (catalog_source is recoverable from OVERLAY_FACT_PROPOSED.payload['catalog_object_ref']),
-- and overlay_catalog_object from the adapter (drift stage). This file TRUNCATEs them and resets the
-- 'overlay' projection checkpoint to 0; the DEPLOY RUNBOOK must then replay the OverlayProjection to
-- head (rebuild_projection / run_projection until projection_lag('overlay')==0) and health-check
-- checkpoint_seq==head_seq BEFORE resuming read traffic (until then resolve_fact fails closed for
-- previously-VERIFIED facts). Applied ONCE by the checksummed schema_migrations ledger.

-- Empty first (so ADD COLUMN NOT NULL is legal, and no stale source-less rows survive).
TRUNCATE overlay_fact_state, overlay_proposal, overlay_fact_dependency, overlay_catalog_object;

ALTER TABLE overlay_fact_state      ADD COLUMN IF NOT EXISTS catalog_source text NOT NULL;
ALTER TABLE overlay_proposal        ADD COLUMN IF NOT EXISTS catalog_source text NOT NULL;
ALTER TABLE overlay_fact_dependency ADD COLUMN IF NOT EXISTS catalog_source text NOT NULL;
ALTER TABLE overlay_catalog_object  ADD COLUMN IF NOT EXISTS catalog_source text NOT NULL;

-- Dependency index keyed on the pair.
ALTER TABLE overlay_fact_dependency DROP CONSTRAINT IF EXISTS overlay_fact_dependency_pkey;
ALTER TABLE overlay_fact_dependency ADD PRIMARY KEY (fact_key, catalog_source, ref_object);
DROP INDEX IF EXISTS overlay_fact_dependency_ref_idx;
CREATE INDEX IF NOT EXISTS overlay_fact_dependency_ref_idx
    ON overlay_fact_dependency (catalog_source, ref_object);

-- Drift snapshot keyed on the pair (two catalogs' same-named objects are distinct rows).
ALTER TABLE overlay_catalog_object DROP CONSTRAINT IF EXISTS overlay_catalog_object_pkey;
ALTER TABLE overlay_catalog_object ADD PRIMARY KEY (catalog_source, object_ref);

-- Force a full replay through the updated OverlayProjection (repopulates source-qualified rows).
UPDATE projection_checkpoints SET checkpoint_seq = 0, head_seq = 0 WHERE projection_name = 'overlay';
